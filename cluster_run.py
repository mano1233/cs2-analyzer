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
import io
import json
import os
import pathlib
import sys
import traceback
import urllib.error
import urllib.request

import boto3
import zstandard

import analyze

FACEIT_API = "https://open.faceit.com/data/v4"
BUCKET = os.environ["R2_BUCKET"]
ENDPOINT = os.environ["R2_ENDPOINT"]
NICKNAME = os.environ.get("FACEIT_NICKNAME", "mirithefish")
API_KEY = os.environ.get("FACEIT_API_KEY", "")
SCRATCH = pathlib.Path(os.environ.get("SCRATCH", "/scratch"))
MAX_PER_RUN = int(os.environ.get("MAX_PER_RUN", "5"))
HISTORY_LIMIT = int(os.environ.get("FACEIT_HISTORY_LIMIT", "20"))

STATE_KEY = "state/processed.json"
RESULTS_KEY = "results/results.json"
TREND_KEY = "results/trend.csv"
DEMO_PREFIX = "demos/"

TREND_FIELDS = ["date", "source", "map", "rounds", "result", "moving_1st_shot_pct",
                "accuracy_pct", "adr", "first_duels_won_pct", "flash_hit_pct",
                "util_per_round", "util_used_pct", "t_util_time_s", "demo"]

s3 = boto3.client("s3", endpoint_url=ENDPOINT, region_name="auto")


def log(msg):
    print("%s  %s" % (dt.datetime.now(dt.timezone.utc).strftime("%H:%M:%S"), msg), flush=True)


def get_json(key, default):
    try:
        return json.loads(s3.get_object(Bucket=BUCKET, Key=key)["Body"].read())
    except s3.exceptions.NoSuchKey:
        return default
    except Exception as e:
        log("could not read %s (%r) - treating as empty" % (key, e))
        return default


def put_json(key, obj):
    s3.put_object(Bucket=BUCKET, Key=key, Body=json.dumps(obj, indent=1, default=float).encode(),
                  ContentType="application/json")


def list_keys(prefix):
    keys, token = [], None
    while True:
        kw = {"Bucket": BUCKET, "Prefix": prefix}
        if token:
            kw["ContinuationToken"] = token
        page = s3.list_objects_v2(**kw)
        keys += [o["Key"] for o in page.get("Contents", [])]
        if not page.get("IsTruncated"):
            return keys
        token = page.get("NextContinuationToken")


def faceit(path):
    req = urllib.request.Request(FACEIT_API + path, headers={"Authorization": "Bearer " + API_KEY})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def fetch_faceit(state):
    """Download demos for matches not yet seen. Returns list of new R2 demo keys."""
    if not API_KEY:
        log("FACEIT_API_KEY empty - skipping FACEIT fetch")
        return []
    done = set(state.get("faceit_matches", []))
    try:
        player = faceit("/players?nickname=%s&game=cs2" % NICKNAME)
        history = faceit("/players/%s/history?game=cs2&offset=0&limit=%d"
                         % (player["player_id"], HISTORY_LIMIT))
    except urllib.error.HTTPError as e:
        log("FACEIT API error %s %s" % (e.code, e.reason))
        return []
    except Exception as e:
        log("FACEIT API unreachable: %r" % (e,))
        return []

    added = []
    for item in history.get("items", []):
        mid = item.get("match_id")
        if not mid or mid in done or len(added) >= MAX_PER_RUN:
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
        try:
            urllib.request.urlretrieve(urls[0], local)
            s3.upload_file(str(local), BUCKET, key)
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


def append_trend(rows):
    if not rows:
        return
    try:
        existing = s3.get_object(Bucket=BUCKET, Key=TREND_KEY)["Body"].read().decode()
    except Exception:
        existing = ""
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=TREND_FIELDS)
    if existing.strip():
        buf.write(existing if existing.endswith("\n") else existing + "\n")
    else:
        w.writeheader()
    for row in rows:
        w.writerow(row)
    s3.put_object(Bucket=BUCKET, Key=TREND_KEY, Body=buf.getvalue().encode(), ContentType="text/csv")


def main():
    SCRATCH.mkdir(parents=True, exist_ok=True)
    state = get_json(STATE_KEY, {})
    seen = set(state.get("demos", []))
    results = get_json(RESULTS_KEY, [])

    fetch_faceit(state)
    put_json(STATE_KEY, state)   # persist match ids even if parsing later fails

    pending = [k for k in list_keys(DEMO_PREFIX) if k not in seen][:MAX_PER_RUN]
    log("%d demo(s) to parse" % len(pending))

    rows = []
    for key in pending:
        archive = SCRATCH / pathlib.Path(key).name
        dem = None
        try:
            s3.download_file(BUCKET, key, str(archive))
            dem = unpack(archive) if archive.suffix in (".zst", ".bz2") else archive
            log("parsing %s (%.0f MB unpacked)" % (key, dem.stat().st_size / 1e6))
            match = analyze.analyze(dem)
            match["source_key"] = key
            results.append(match)
            row = row_for(match, key)
            if row:
                rows.append(row)
                log("  %s %s  moving %s%%  util/r %s" % (row["map"], row["result"],
                                                         row["moving_1st_shot_pct"],
                                                         row["util_per_round"]))
            else:
                log("  %s not in this demo" % analyze.ME)
            seen.add(key)
        except Exception:
            log("failed on %s\n%s" % (key, traceback.format_exc()))
        finally:
            for f in (archive, dem):
                if f is not None:
                    pathlib.Path(f).unlink(missing_ok=True)

    put_json(RESULTS_KEY, results)
    append_trend(rows)
    state["demos"] = sorted(seen)
    put_json(STATE_KEY, state)
    log("done: %d parsed this run, %d tracked overall" % (len(rows), len(seen)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
