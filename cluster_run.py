"""One CronJob run: fetch new CS2 demos, parse them, write results back to R2.

Flow per run:
  1. read state/processed.json from R2 (which matches and demos are already done)
  2. ask the FACEIT API for the player's recent matches, download the new demos
  3. also pick up demos uploaded to demos/ by anything else (e.g. Premier demos)
  4. parse one demo at a time in scratch, deleting each before the next
  5. merge into results/results.json and append rows to results/trend.csv

Scratch disk is the binding constraint, not CPU: a demo is ~200 MB compressed and
~285 MB unpacked, so the loop never holds more than one of each. Demo objects age out
of the bucket by lifecycle rule; the parsed results stay.
"""
import csv
import datetime as dt
import functools
import io
import json
import os
import pathlib
import ssl
import sys
import traceback
import urllib.error
import urllib.parse
import urllib.request

import boto3
import zstandard

import analyze
import artifacts
import faceit_stats
import rollup
import report

FACEIT_API = "https://open.faceit.com/data/v4"
BUCKET = os.environ.get("R2_BUCKET", "")
ENDPOINT = os.environ.get("R2_ENDPOINT", "")
NICKNAME = os.environ.get("FACEIT_NICKNAME", "mirithefish")
API_KEY = os.environ.get("FACEIT_API_KEY", "")
# The Data API's demo_url is a private resource URL whose host does not resolve. It
# must be exchanged for a signed URL through the Downloads API, which needs its own
# token (application form, ~30 day wait). Without it, demos can only arrive by being
# uploaded to the bucket - which this job parses just the same.
DOWNLOADS_TOKEN = os.environ.get("FACEIT_DOWNLOADS_TOKEN", "")
# Demo pulling is off by default: it cannot work without Downloads API access, and
# demos arrive by upload instead. Stats need no such permission, so they stay on.
FETCH_DEMOS = os.environ.get("FACEIT_FETCH_DEMOS", "0").lower() in ("1", "true", "yes")
# Deleting a parsed demo is the only thing that removes data from the bucket, so it is
# off unless asked for, and it happens here rather than as a lifecycle rule on the
# prefix: a rule sweeps by age and cannot tell a demo from a stats file that moved.
# Keeping demos is what makes a metric fix applicable to history - a demo that is gone
# can never be re-parsed, so every future metric starts from the day it shipped.
DELETE_PARSED_DEMOS = os.environ.get("DELETE_PARSED_DEMOS", "0").lower() in ("1", "true", "yes")
FETCH_STATS = os.environ.get("FACEIT_FETCH_STATS", "1").lower() in ("1", "true", "yes")
DOWNLOADS_API = "https://open.faceit.com/download/v2/demos/download"
SCRATCH = pathlib.Path(os.environ.get("SCRATCH", "/scratch"))
MAX_PER_RUN = int(os.environ.get("MAX_PER_RUN", "5"))
HISTORY_PAGE = int(os.environ.get("FACEIT_HISTORY_PAGE", "50"))
# The job now runs every 15 minutes so an uploaded demo is parsed promptly, but the
# FACEIT stats only change when a match ends - syncing them every run would mean ~96
# passes over the window a day for no new data.
STATS_MIN_INTERVAL = int(os.environ.get("FACEIT_STATS_MIN_INTERVAL_MIN", "60")) * 60
WINDOW_DAYS = int(os.environ.get("FACEIT_WINDOW_DAYS", "21"))
REPORT_DIR = pathlib.Path(os.environ.get("REPORT_DIR", "/tmp/report"))
REPORT_CONFIGMAP = os.environ.get("REPORT_CONFIGMAP", "")

SA = pathlib.Path("/var/run/secrets/kubernetes.io/serviceaccount")

STATE_KEY = "state/processed.json"
STATS_KEY = "results/faceit_stats.json"
IMAGE = os.environ.get("IMAGE_TAG", "")
PLAYER_TABLE_KEY = "analytics/player_match.parquet"
MATCH_TABLE_KEY = "analytics/match.parquet"
RESULTS_KEY = "results/results.json"
TREND_KEY = "results/trend.csv"
DEMO_PREFIX = "demos/"

TREND_FIELDS = ["date", "source", "map", "rounds", "result", "moving_1st_shot_pct",
                "accuracy_pct", "adr", "first_duels_won_pct", "flash_hit_pct",
                "util_per_round", "util_used_pct", "t_util_time_s", "demo"]

@functools.lru_cache(maxsize=1)
def s3():
    """Built on first use, not at import: the endpoint is only valid in the cluster."""
    return boto3.client("s3", endpoint_url=ENDPOINT, region_name="auto")


def log(msg):
    print("%s  %s" % (dt.datetime.now(dt.timezone.utc).strftime("%H:%M:%S"), msg), flush=True)


def get_json(key, default):
    try:
        return json.loads(s3().get_object(Bucket=BUCKET, Key=key)["Body"].read())
    except s3().exceptions.NoSuchKey:
        return default
    except Exception as e:
        log("could not read %s (%r) - treating as empty" % (key, e))
        return default


def put_json(key, obj):
    s3().put_object(Bucket=BUCKET, Key=key, Body=json.dumps(obj, indent=1, default=float).encode(),
                  ContentType="application/json")


def list_keys(prefix):
    keys, token = [], None
    while True:
        kw = {"Bucket": BUCKET, "Prefix": prefix}
        if token:
            kw["ContinuationToken"] = token
        page = s3().list_objects_v2(**kw)
        keys += [o["Key"] for o in page.get("Contents", [])]
        if not page.get("IsTruncated"):
            return keys
        token = page.get("NextContinuationToken")


def match_key(match):
    """What identifies a parsed match: the object it came from, falling back to the
    demo filename for entries written before source_key existed."""
    return match.get("source_key") or match.get("demo") or ""


def demo_keys():
    """Demos under the prefix, and nothing else.

    Creating a folder in the R2 console leaves a zero-byte object named "demos/". Handed
    to the parser that is an empty file, and the parser does not raise on one - it panics
    in Rust, which arrives as a PanicException and takes the process with it.
    """
    return [k for k in list_keys(DEMO_PREFIX)
            if k != DEMO_PREFIX and k.endswith((".dem", ".dem.zst", ".dem.bz2"))]


def faceit(path):
    req = urllib.request.Request(FACEIT_API + path, headers={"Authorization": "Bearer " + API_KEY})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def faceit_history():
    """Matches finished inside the window, newest first, following pagination.

    A window rather than a fixed count: "the last three weeks" is what a person means
    by recent form, and it stays right whether that was 3 matches or 30."""
    now = int(dt.datetime.now(dt.timezone.utc).timestamp())
    since = now - WINDOW_DAYS * 24 * 3600
    player = faceit("/players?nickname=%s&game=cs2" % NICKNAME)
    items, offset = [], 0
    while True:
        page = faceit("/players/%s/history?game=cs2&from=%d&to=%d&offset=%d&limit=%d"
                      % (player["player_id"], since, now, offset, HISTORY_PAGE))
        batch = page.get("items", [])
        items += batch
        # FACEIT returns an empty page rather than a total, so stop on a short page.
        if len(batch) < HISTORY_PAGE:
            return items
        offset += HISTORY_PAGE
        if offset >= 500:   # guard against a pathological loop
            return items


def signed_url(resource_url):
    """Exchange a private resource URL for a temporary signed download URL."""
    req = urllib.request.Request(
        DOWNLOADS_API, method="POST",
        data=json.dumps({"resource_url": resource_url}).encode(),
        headers={"Authorization": "Bearer " + DOWNLOADS_TOKEN,
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        payload = json.loads(r.read())
    return payload.get("payload", {}).get("download_url") or payload.get("download_url")


def fetch_faceit(state):
    """Download demos for matches not yet seen. Returns list of new R2 demo keys."""
    if not FETCH_DEMOS:
        log("demo fetching disabled (FACEIT_FETCH_DEMOS); demos come from uploads")
        return []
    if not API_KEY:
        log("FACEIT_API_KEY empty - skipping FACEIT fetch")
        return []
    if not DOWNLOADS_TOKEN:
        log("no FACEIT_DOWNLOADS_TOKEN: demo URLs from the Data API are private and "
            "cannot be fetched. Upload demos to the bucket's demos/ prefix instead.")
        return []
    done = set(state.get("faceit_matches", []))
    try:
        history = faceit_history()
    except urllib.error.HTTPError as e:
        log("FACEIT API error %s %s" % (e.code, e.reason))
        return []
    except Exception as e:
        log("FACEIT API unreachable: %r" % (e,))
        return []
    log("%d match(es) in the last %d days" % (len(history), WINDOW_DAYS))

    added, attempted = [], 0
    for item in history:
        mid = item.get("match_id")
        if not mid or mid in done or attempted >= MAX_PER_RUN:
            continue
        try:
            urls = faceit("/matches/%s" % mid).get("demo_url") or []
        except Exception as e:
            log("no details for %s: %r" % (mid, e))
            continue
        if not urls:
            log("%s has no demo (pruned by FACEIT)" % mid)
            done.add(mid)
            continue
        key = DEMO_PREFIX + mid + ".dem.zst"
        local = SCRATCH / (mid + ".dem.zst")
        attempted += 1
        try:
            url = signed_url(urls[0])
            if not url:
                log("no download_url returned for %s" % mid)
                continue
            host = urllib.parse.urlsplit(url).hostname
            urllib.request.urlretrieve(url, local)
            s3().upload_file(str(local), BUCKET, key)
            log("stored %s (%.0f MB)" % (key, local.stat().st_size / 1e6))
            added.append(key)
            done.add(mid)
        except Exception as e:
            log("fetch failed for %s: %r" % (mid, e))
        finally:
            local.unlink(missing_ok=True)
    state["faceit_matches"] = sorted(done)
    return added


def unpack(archive):
    dem = archive.with_suffix("")
    with archive.open("rb") as src, dem.open("wb") as dst:
        if archive.suffix == ".zst":
            zstandard.ZstdDecompressor().copy_stream(src, dst)
        else:
            import bz2
            dst.write(bz2.decompress(src.read()))
    return dem


def row_for(match, key):
    me = match["players"].get(analyze.ME)
    if not me:
        return None
    r = analyze.rates(me)
    won = int(me.get("rounds_won", 0))
    num = lambda v: round(v, 2) if v == v else ""   # NaN -> blank
    return {
        "date": dt.date.today().isoformat(),
        "source": "premier" if "match730" in key else "faceit",
        "map": match["map"],
        "rounds": match["rounds"],
        "result": "%d-%d" % (won, match["rounds"] - won),
        "moving_1st_shot_pct": num(r["moving 1st shot%"]),
        "accuracy_pct": num(r["accuracy%"]),
        "adr": num(r["ADR"]),
        "first_duels_won_pct": num(100 * me["open_won"] / max(me["open_won"] + me["open_lost"], 1)),
        "flash_hit_pct": num(r["flash hit% (>=1 enemy)"]),
        "util_per_round": num(r["util thrown/r"]),
        "util_used_pct": num(r["util used% of owned"]),
        "t_util_time_s": num(r["avg util time T (s)"]),
        "demo": pathlib.Path(key).name,
    }


def merge_trend(existing, rows):
    """The trend CSV with `rows` merged in, one row per demo, oldest first.

    Keyed rather than appended: re-parsing a demo after a metric fix should correct its
    row, not add a second one claiming something different about the same match.
    """
    by_demo = {}
    for row in csv.DictReader(io.StringIO(existing)) if existing.strip() else ():
        by_demo[row.get("demo")] = {f: row.get(f, "") for f in TREND_FIELDS}
    for row in rows:
        by_demo[row.get("demo")] = row
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=TREND_FIELDS)
    w.writeheader()
    for row in by_demo.values():
        w.writerow(row)
    return buf.getvalue()


def append_trend(rows):
    if not rows:
        return
    try:
        existing = s3().get_object(Bucket=BUCKET, Key=TREND_KEY)["Body"].read().decode()
    except Exception:
        existing = ""
    s3().put_object(Bucket=BUCKET, Key=TREND_KEY,
                    Body=merge_trend(existing, rows).encode(), ContentType="text/csv")


def sync_stats(state):
    """Per-match stats from the API. Independent of demos: this is what keeps the trend
    current when no demo has been uploaded. Rate-limited to STATS_MIN_INTERVAL."""
    if not (FETCH_STATS and API_KEY):
        return []
    known = get_json(STATS_KEY, [])
    now = int(dt.datetime.now(dt.timezone.utc).timestamp())
    last = int(state.get("last_stats_sync", 0))
    if now - last < STATS_MIN_INTERVAL:
        log("stats synced %d min ago - skipping (min interval %d min)"
            % ((now - last) // 60, STATS_MIN_INTERVAL // 60))
        return known
    state["last_stats_sync"] = now
    seen = {r.get("match_id") for r in known}
    try:
        fresh = faceit_stats.collect(NICKNAME, API_KEY, WINDOW_DAYS, log=log, seen=seen)
    except Exception as e:
        log("FACEIT stats sync failed: %r" % (e,))
        return known
    if fresh:
        # The stat keys are undocumented and have changed before, so log what FACEIT
        # actually returned. This is the only place the real schema is visible.
        keys = sorted({k for row in fresh for k in row.get("stats", {})})
        log("FACEIT stats: %d key(s) returned: %s" % (len(keys), ", ".join(keys)))
        sample = next((r for r in fresh if r.get("stats")), None)
        if sample:
            shown = ", ".join("%s=%s" % (k, sample["stats"][k])
                              for k in sorted(sample["stats"])[:40])
            log("FACEIT stats: sample %s on %s -> %s"
                % (sample.get("nickname"), sample.get("map"), shown))
        unmatched = sorted(set(keys) - {k for _, cands in faceit_stats.CURATED for k in cands})
        if unmatched:
            log("FACEIT stats: not in the curated view: %s" % ", ".join(unmatched))
        known = known + fresh
        put_json(STATS_KEY, known)
    return known


def write_artifacts(match, key, stats):
    """Canonical per-match files. Written before anything derived, so a later rollup or
    render can be rebuilt from them without the demo.

    Returns whether they landed: nothing deletes a demo whose artifacts did not.
    """
    match_id = artifacts.match_id_from(key)
    rows = [r for r in (stats or []) if r.get("match_id") == match_id]
    finished = rows[0].get("finished_at") if rows else None
    mtime = None
    try:
        head = s3().head_object(Bucket=BUCKET, Key=key)
        mtime = int(head["LastModified"].timestamp())
    except Exception:
        pass
    try:
        files = artifacts.build(match, match_id, key, stats_rows=rows,
                                finished_at=finished, mtime=mtime, image=IMAGE)
        prefix = artifacts.prefix_for(match_id, artifacts.match_date(match, finished, mtime))
        for name, obj in files.items():
            put_json(prefix + name, obj)
        log("  wrote %d artifact file(s) under %s" % (len(files), prefix))
        return True
    except Exception:
        log("  artifact write failed: %s" % traceback.format_exc().splitlines()[-1])
        return False


def write_rollup(results, stats):
    """Rebuild the analytics tables from every parsed match.

    Rebuilt rather than appended: a metric fix should correct history, and the whole
    table is a few thousand rows.
    """
    try:
        players, matches = rollup.build(results, stats)
        s3().put_object(Bucket=BUCKET, Key=PLAYER_TABLE_KEY, Body=players,
                        ContentType="application/vnd.apache.parquet")
        s3().put_object(Bucket=BUCKET, Key=MATCH_TABLE_KEY, Body=matches,
                        ContentType="application/vnd.apache.parquet")
        log("rollup: %s (%.0f KB) + %s (%.0f KB)"
            % (PLAYER_TABLE_KEY, len(players) / 1000, MATCH_TABLE_KEY, len(matches) / 1000))
    except Exception:
        log("rollup failed: %s" % traceback.format_exc().splitlines()[-1])


def publish_report(results, stats=()):
    """Render the pages and push them into the ConfigMap the web pod mounts.

    Kubelet re-syncs mounted ConfigMaps on its own, so the served pages update without
    restarting anything. Raw HTTP against the API server rather than the kubernetes
    client library: it is one PATCH, and the in-cluster token and CA are right there.
    """
    files = report.render(results, REPORT_DIR, stats=list(stats))
    log("rendered %s" % ", ".join(f.name for f in files))
    if not REPORT_CONFIGMAP:
        return
    payload = {f.name: f.read_text(encoding="utf-8") for f in files}
    size = sum(len(v.encode()) for v in payload.values())
    if size > 900_000:      # ConfigMaps cap at ~1 MiB including keys and metadata
        log("report is %.0f KB, too close to the ConfigMap limit - not publishing" % (size / 1000))
        return
    try:
        namespace = (SA / "namespace").read_text().strip()
        token = (SA / "token").read_text().strip()
    except OSError as e:
        log("no service account mounted (%r) - skipping ConfigMap update" % (e,))
        return
    url = ("https://kubernetes.default.svc/api/v1/namespaces/%s/configmaps/%s"
           % (namespace, REPORT_CONFIGMAP))
    # A JSON Patch that replaces /data outright, rather than a merge patch that only
    # adds and overwrites. A merge left every page we stopped generating served forever,
    # frozen at whatever it said the day it was dropped - team.html outlived the redesign
    # that deleted it. "add" on an existing member replaces it (RFC 6902) and creates it
    # when the ConfigMap has no data yet, so one op covers both. Only /data is touched,
    # so the labels and annotations Terraform owns are left alone.
    body = [{"op": "add", "path": "/data", "value": payload}]
    req = urllib.request.Request(
        url, method="PATCH", data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + token,
                 "Content-Type": "application/json-patch+json"})
    ctx = ssl.create_default_context(cafile=str(SA / "ca.crt"))
    try:
        with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
            log("updated ConfigMap %s/%s (%s, %.0f KB)"
                % (namespace, REPORT_CONFIGMAP, r.status, size / 1000))
    except urllib.error.HTTPError as e:
        log("ConfigMap update failed %s: %s" % (e.code, e.read()[:300]))
    except Exception as e:
        log("ConfigMap update failed: %r" % (e,))


def main():
    if not BUCKET or not ENDPOINT:
        log("R2_BUCKET and R2_ENDPOINT must be set")
        return 2
    SCRATCH.mkdir(parents=True, exist_ok=True)
    state = get_json(STATE_KEY, {})
    seen = set(state.get("demos", []))
    # Keyed by object, not appended: re-parsing a demo should replace its entry, so a
    # metric added to the parser can be backfilled by clearing `seen` and letting the
    # runs catch up. Appending made results.json a mix of schema generations instead.
    results = {match_key(m): m for m in get_json(RESULTS_KEY, [])}

    fetch_faceit(state)
    put_json(STATE_KEY, state)   # persist match ids even if parsing later fails
    stats = sync_stats(state)

    pending = [k for k in demo_keys() if k not in seen][:MAX_PER_RUN]
    log("%d demo(s) to parse" % len(pending))

    rows = []
    for key in pending:
        archive = SCRATCH / pathlib.Path(key).name
        dem = None
        try:
            s3().download_file(BUCKET, key, str(archive))
            dem = unpack(archive) if archive.suffix in (".zst", ".bz2") else archive
            log("parsing %s (%.0f MB unpacked)" % (key, dem.stat().st_size / 1e6))
            match = analyze.analyze(dem)
            match["source_key"] = key
            results[match_key(match)] = match
            archived = write_artifacts(match, key, stats)
            row = row_for(match, key)
            if row:
                rows.append(row)
                log("  %s %s  moving %s%%  util/r %s" % (row["map"], row["result"],
                                                         row["moving_1st_shot_pct"],
                                                         row["util_per_round"]))
            else:
                log("  %s not in this demo" % analyze.ME)
            seen.add(key)
            if DELETE_PARSED_DEMOS and archived:
                s3().delete_object(Bucket=BUCKET, Key=key)
                log("  deleted %s (DELETE_PARSED_DEMOS)" % key)
        except BaseException as e:
            # Not `except Exception`: demoparser2 is a Rust extension, and a malformed
            # demo panics rather than raising. pyo3 surfaces that as a PanicException,
            # which inherits from BaseException - so one bad file used to kill the run
            # before any of the matches parsed before it were saved.
            if isinstance(e, (KeyboardInterrupt, SystemExit)):
                raise
            log("failed on %s\n%s" % (key, traceback.format_exc()))
        finally:
            for f in (archive, dem):
                if f is not None:
                    pathlib.Path(f).unlink(missing_ok=True)

    parsed = list(results.values())
    put_json(RESULTS_KEY, parsed)
    append_trend(rows)
    write_rollup(parsed, stats)
    publish_report(parsed, stats)
    state["demos"] = sorted(seen)
    put_json(STATE_KEY, state)
    log("done: %d parsed this run, %d tracked overall" % (len(rows), len(seen)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
