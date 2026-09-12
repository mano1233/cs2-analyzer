"""Derived analytics tables, rebuilt every run from the parsed matches.

Two parquet files:

  analytics/player_match.parquet   one row per (match, player, stat_source, scope, metric)
  analytics/match.parquet          one row per (match, metric) for team-level facts

Tidy (long) rather than wide on purpose: FACEIT renames stat keys, and new metrics get
added here regularly. In long form that is new rows; in wide form it is a schema
migration every time.

Nothing is merged across sources. A row carries `stat_source` = demo or faceit, because
FACEIT's "flash success" and our "flash hit" measure different things and averaging them
produces a number nobody can explain.
"""
import io
import re

import pyarrow as pa
import pyarrow.parquet as pq

import analyze
import artifacts
import faceit_stats

SCOPES = (("all", ""), ("t", "t_"), ("ct", "ct_"), ("h1", "h1_"), ("h2", "h2_"), ("ot", "ot_"))

# Human labels make bad SQL identifiers. These are the stable machine names.
_REPLACEMENTS = (
    ("%", "_pct"), ("/r", "_per_round"), ("->", "_to_"), (">=", "at_least"),
    ("+", "_plus_"), ("&", "_and_"),
)

SCHEMA = pa.schema([
    ("date", pa.string()), ("match_id", pa.string()), ("map", pa.string()),
    ("source", pa.string()), ("player", pa.string()), ("team", pa.string()),
    ("stat_source", pa.string()), ("scope", pa.string()),
    ("metric", pa.string()), ("value", pa.float64()),
])

MATCH_SCHEMA = pa.schema([
    ("date", pa.string()), ("match_id", pa.string()), ("map", pa.string()),
    ("source", pa.string()), ("scope", pa.string()),
    ("metric", pa.string()), ("value", pa.float64()),
])


def metric_key(label):
    """"moving 1st shot%" -> "moving_1st_shot_pct". Stable, lowercase, SQL-safe."""
    text = str(label).strip().lower()
    for old, new in _REPLACEMENTS:
        text = text.replace(old, new)
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return re.sub(r"_+", "_", text).strip("_")


def _numeric(value):
    """Numbers only. Strings like "10/15" are labels, and NaN means "no data" - both are
    omitted rather than stored, so a query never has to filter out non-measurements."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if value == value else None


def player_rows(match, match_id, date, stats_rows=(), tracked=None):
    source = artifacts.source_of(match.get("demo", ""))
    mapname = match.get("map")
    keep = set(tracked if tracked is not None else analyze.tracked_in(match))
    started = match.get("started_side", {})
    by_player = {}
    for row in stats_rows or ():
        by_player.setdefault(row.get("nickname"), []).append(row)

    rows = []
    for name, counters in match.get("players", {}).items():
        if name not in keep:
            continue
        team = "T" if started.get(name) == analyze.T_SIDE else "CT"
        base = {"date": date, "match_id": match_id, "map": mapname, "source": source,
                "player": name, "team": team}

        for scope, prefix in SCOPES:
            rates = analyze.scope_rates(counters, prefix)
            if not rates["rounds"]:
                continue
            if prefix == "":
                # scope_rates only carries what splits by side or half. The rest -
                # the whole utility family, flash conversion, distance at death -
                # lives in rates(), and without this the parquet could not answer
                # questions the player pages already answer.
                rates = dict(rates, **analyze.rates(counters))
            for label, value in rates.items():
                number = _numeric(value)
                if number is None:
                    continue
                rows.append(dict(base, stat_source="demo", scope=scope,
                                 metric=metric_key(label), value=number))
            # raw counters too: rates answer "how good", counters answer "how much"
            for label, value in counters.items():
                if not label.startswith(prefix) or (prefix == "" and "_" in label[:3]):
                    continue
                number = _numeric(value)
                if number is None:
                    continue
                rows.append(dict(base, stat_source="demo", scope=scope,
                                 metric="count_" + metric_key(label[len(prefix):]), value=number))

        for api_row in by_player.get(name, []):
            for label, value in faceit_stats.curated(api_row.get("stats", {})):
                number = _numeric(faceit_stats.as_number(value))
                if number is None:
                    continue
                rows.append(dict(base, stat_source="faceit", scope="all",
                                 metric=metric_key(label), value=number))
    return rows


def match_rows(match, match_id, date):
    """Team-level facts: opening-kill conversion, post-plant, economy buckets."""
    source = artifacts.source_of(match.get("demo", ""))
    base = {"date": date, "match_id": match_id, "map": match.get("map"), "source": source}
    rows = []
    for label, value in analyze.round_metrics([match]).items():
        number = _numeric(value)
        if number is None:
            continue
        rows.append(dict(base, scope="match", metric=metric_key(label), value=number))
    return rows


def to_parquet(rows, schema):
    """Parquet bytes, ready to upload. Empty input still produces a valid file with the
    schema, so a query against a fresh bucket returns zero rows rather than failing."""
    columns = {field.name: [row.get(field.name) for row in rows] for field in schema}
    table = pa.table(columns, schema=schema)
    buffer = io.BytesIO()
    pq.write_table(table, buffer, compression="zstd")
    return buffer.getvalue()


def build(results, stats_rows=(), date_for=None):
    """(player_bytes, match_bytes) for every parsed match."""
    players, matches = [], []
    for match in results:
        key = match.get("source_key", match.get("demo", ""))
        match_id = artifacts.match_id_from(key)
        date = (date_for or (lambda m, i: artifacts.match_date(m)))(match, match_id)
        rows = [r for r in (stats_rows or ()) if r.get("match_id") == match_id]
        players += player_rows(match, match_id, date, rows)
        matches += match_rows(match, match_id, date)
    return to_parquet(players, SCHEMA), to_parquet(matches, MATCH_SCHEMA)
