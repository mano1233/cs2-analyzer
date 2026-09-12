"""Canonical per-match artifacts: the files everything else is rebuilt from.

Layout, one prefix per match:

    matches/<YYYY-MM-DD>/<match_id>/
      metadata.json          when, where, who, links, provenance
      teams.json             per-team aggregates and the pairing matrices
      players/<slug>.json    one file per player, demo and API stats side by side

Date-first so listings sort chronologically. Nickname slugs are unique *within* a
match so they stay readable here, while stable ids live inside the files - a nickname
change never breaks history.

Pure: builds a {relative path: object} mapping and writes nothing itself, so the
caller decides whether that goes to R2 or a local directory, and tests need no bucket.
"""
import datetime as dt
import re

import analyze
import faceit_stats

# 2: pairs gained tradeable and death_dist_*, and the economy thresholds grew into the
# full conventions block - every threshold the numbers in this file depend on.
SCHEMA_VERSION = 2
FACEIT_ROOM = "https://www.faceit.com/en/cs2/room/%s"
SIDE_NAMES = {analyze.T_SIDE: "T", analyze.CT_SIDE: "CT"}


def slug(nickname):
    """Filename-safe, readable, and confined to this match's prefix."""
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "-", str(nickname)).strip("-").lower()
    return cleaned or "player"


def match_date(match, finished_at=None, mtime=None):
    """The match's day, best source first: API finish time, object mtime, today."""
    for value in (finished_at, mtime):
        if value:
            return dt.datetime.fromtimestamp(int(value), dt.timezone.utc).date().isoformat()
    return dt.date.today().isoformat()


def premier_ids(demo_name):
    """match730_<matchid>_<outcomeid>_<token>.dem carries the three ids a CS2 share
    code is built from. Stored raw: deriving the code is a separate job, and inventing
    a link would be worse than admitting there is none."""
    m = re.match(r"match730_(\d+)_(\d+)_(\d+)", str(demo_name))
    if not m:
        return None
    return {"matchid": m.group(1), "outcomeid": m.group(2), "token": m.group(3)}


def source_of(key_or_name):
    return "premier" if "match730" in str(key_or_name) else "faceit"


def match_id_from(key_or_name):
    """The match id an object key belongs to.

    FACEIT names a demo <match_id>-<map>-<part>.dem, so the filename carries a "-1-1"
    the API's match_id does not. Derived ids kept that suffix, so every join against the
    stats returned nothing: no faceit block in any player file, no faceit rows in the
    rollup, and finished_at always null. Premier names have no such suffix and are left
    alone.
    """
    stem = str(key_or_name).split("/")[-1].split(".dem")[0]
    if source_of(stem) == "premier":
        return stem
    return re.sub(r"-\d+-\d+$", "", stem)


def build(match, match_id, source_key, stats_rows=(), finished_at=None, mtime=None,
          image=None, demo_available=None, tracked=None):
    """Return {relative path: json-able object} for one match.

    `tracked` limits the per-player files to our own roster; the opponents still
    contribute to the lobby baseline in teams.json, they just do not get a file each.
    """
    source = source_of(source_key)
    date = match_date(match, finished_at, mtime)
    steamids = match.get("steamids", {})
    players = match.get("players", {})
    round_table = match.get("round_table", [])

    # Split by the side held in round 1. Per-player t_rounds/ct_rounds cannot do this:
    # sides swap at the half, so everyone is ~12/12 and all ten land on one team.
    started = match.get("started_side", {})
    by_team = {}
    for name in players:
        side = started.get(name)
        if side is None:      # joined late; put them with whichever side they played more
            c = players[name]
            side = analyze.T_SIDE if c.get("t_rounds", 0) >= c.get("ct_rounds", 0) else analyze.CT_SIDE
        by_team.setdefault(int(side), []).append(name)

    stack = set(tracked if tracked is not None else analyze.tracked_in(match))
    faceit_by_player = {row.get("nickname"): row for row in stats_rows or ()}

    metadata = {
        "schema_version": SCHEMA_VERSION,
        "match_id": match_id,
        "date": date,
        "finished_at": int(finished_at) if finished_at else None,
        "map": match.get("map"),
        "source": source,
        "rounds": match.get("rounds"),
        "demo": {
            "file": match.get("demo"),
            "object": source_key,
            "faceit_url": FACEIT_ROOM % match_id if source == "faceit" else None,
            # DEV-59 decides availability; unknown until then rather than assumed true.
            "available": demo_available,
            "checked_at": None,
        },
        "premier_ids": premier_ids(match.get("demo")),
        "teams": [
            {
                "started_as": SIDE_NAMES.get(side, str(side)),
                "is_our_stack": bool(stack & set(members)),
                "players": [
                    {"nickname": n, "steamid64": steamids.get(n), "in_stack": n in stack}
                    for n in sorted(members)
                ],
                "rounds_won": max((int(players[n].get("rounds_won", 0)) for n in members), default=0),
            }
            for side, members in sorted(by_team.items())
        ],
        "parsed_with": {"image": image, "schema": SCHEMA_VERSION,
                        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")},
    }

    teams = {
        "match_id": match_id,
        "round_metrics": analyze.round_metrics([match]),
        "sites": analyze.plant_sites([match]),
        "round_table": round_table,
        "pairs": {
            "traded_for": match.get("traded_for", {}),
            # The denominator for traded_for: chances, not just conversions.
            "tradeable": match.get("tradeable", {}),
            "flash_conv": match.get("flash_conv", {}),
            "prox_sum": match.get("prox_sum", {}),
            "prox_n": match.get("prox_n", {}),
            "death_dist_sum": match.get("death_dist_sum", {}),
            "death_dist_n": match.get("death_dist_n", {}),
        },
        # Every threshold this file's numbers depend on, so a page can say what it
        # assumed instead of presenting a convention as a measurement.
        "conventions": {
            "eco_below": analyze.ECO_MAX,
            "full_above": analyze.FORCE_MAX,
            "trade_window_s": analyze.TRADE_WINDOW / analyze.TR,
            "trade_range_units": analyze.TRADE_RANGE,
            "trade_range_m": round(analyze.TRADE_RANGE * analyze.UNIT_M, 1),
            "effective_blind_s": analyze.EFFECTIVE_BLIND,
            "moving_above_u_per_s": analyze.MOVING,
        },
        # Everyone else, pooled: keeps the comparison baseline without writing a file
        # per opponent we will never look at individually.
        "lobby_baseline": analyze.rates(analyze.pool(
            c for n, c in players.items() if n not in stack)) if len(players) > len(stack) else None,
        "tracked": sorted(stack),
    }

    out = {"metadata.json": metadata, "teams.json": teams}
    for name, counters in players.items():
        if name not in stack:
            continue
        api_row = faceit_by_player.get(name)
        out["players/%s.json" % slug(name)] = {
            "match_id": match_id,
            "nickname": name,
            "steamid64": steamids.get(name),
            "in_stack": name in stack,
            # Sources stay namespaced: FACEIT's "flash success" and our "flash hit"
            # measure different things, so they are never merged into one number.
            "demo": {
                "counters": counters,
                "rates": analyze.rates(counters),
                "scopes": {scope: analyze.scope_rates(counters, prefix)
                           for scope, prefix in (("all", ""), ("t", "t_"), ("ct", "ct_"),
                                                 ("h1", "h1_"), ("h2", "h2_"), ("ot", "ot_"))},
                "role_signals": analyze.role_signals(counters),
            },
            "faceit": {
                "stats": api_row.get("stats") if api_row else None,
                "curated": dict(faceit_stats.curated(api_row["stats"])) if api_row else None,
            },
        }
    return out


def prefix_for(match_id, date):
    return "matches/%s/%s/" % (date, match_id)
