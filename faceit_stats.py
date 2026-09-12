"""Per-match player stats straight from the FACEIT API - no demo required.

Demo parsing gives the diagnostic metrics (movement on the first shot, trades between
specific players, per-half splits). None of that exists in the API. What the API does
give, nightly and with no manual step, is the scoreboard row plus FACEIT's advanced
stats: utility damage, flashes, entry duels, clutches.

The exact stat keys are not documented and have changed before, so nothing here assumes
a fixed schema: every key is kept as returned, a curated subset is surfaced by
case-insensitive lookup with aliases, and unknown keys still render.
"""
import datetime as dt
import json
import urllib.parse
import urllib.request

API = "https://open.faceit.com/data/v4"

# Curated view: label -> candidate keys, tried in order, case-insensitively.
# The real schema, confirmed from a live run on 2026-09-12 (47 keys). Ordered by what
# the player is working on: aim, then support utility, then entry duels.
CURATED = [
    ("ADR", ["ADR"]),
    ("K/D", ["K/D Ratio"]),
    ("K/R", ["K/R Ratio"]),
    ("HS %", ["Headshots %"]),
    ("Kills", ["Kills"]),
    ("Deaths", ["Deaths"]),
    ("Assists", ["Assists"]),
    ("Utility thrown", ["Utility Count"]),
    ("Utility / round", ["Utility Usage per Round"]),
    ("Utility successes", ["Utility Successes"]),
    ("Utility success %", ["Utility Success Rate per Match"]),
    ("Utility damage", ["Utility Damage"]),
    ("Utility dmg / round", ["Utility Damage per Round in a Match"]),
    ("Enemies hit by utility", ["Utility Enemies"]),
    ("Flashes thrown", ["Flash Count"]),
    ("Flashes / round", ["Flashes per Round in a Match"]),
    ("Flash successes", ["Flash Successes"]),
    ("Flash success %", ["Flash Success Rate per Match"]),
    ("Enemies flashed", ["Enemies Flashed"]),
    ("Enemies flashed / round", ["Enemies Flashed per Round in a Match"]),
    ("Entry attempts", ["Entry Count"]),
    ("Entry wins", ["Entry Wins"]),
    ("Entry success %", ["Match Entry Success Rate"]),
    ("Entries / round", ["Match Entry Rate"]),
    ("First kills", ["First Kills"]),
    ("Clutch kills", ["Clutch Kills"]),
    ("1v1 won", ["1v1Wins"]),
    ("1v1 faced", ["1v1Count"]),
    ("Sniper kills", ["Sniper Kills"]),
    ("MVPs", ["MVPs"]),
]

# These arrive as 0-1 fractions (Entry Rate 0.17 alongside Entry Count 3 over 18
# rounds), unlike "Headshots %" which is already 0-100. Scaled for display only.
PERCENT_KEYS = {
    "match entry success rate", "match entry rate", "flash success rate per match",
    "utility success rate per match", "utility damage success rate per match",
    "sniper kill rate per match", "sniper kill rate per round",
    "match 1v1 win rate", "match 1v2 win rate",
}

# Shown on the summary cards: the metrics tied to the aim and support-utility goals.
HEADLINE = ["ADR", "K/D", "HS %", "Utility / round", "Utility dmg / round",
            "Flash success %", "Enemies flashed / round", "Entry success %", "First kills"]


def _get(path, api_key):
    req = urllib.request.Request(API + path, headers={"Authorization": "Bearer " + api_key})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def player_id(nickname, api_key):
    return _get("/players?nickname=%s&game=cs2" % urllib.parse.quote(nickname), api_key)["player_id"]


def history(pid, api_key, window_days=21, page=50, hard_cap=500):
    """Matches finished inside the window, newest first."""
    now = int(dt.datetime.now(dt.timezone.utc).timestamp())
    since = now - window_days * 24 * 3600
    items, offset = [], 0
    while offset < hard_cap:
        batch = _get("/players/%s/history?game=cs2&from=%d&to=%d&offset=%d&limit=%d"
                     % (pid, since, now, offset, page), api_key).get("items", [])
        items += batch
        if len(batch) < page:
            break
        offset += page
    return items


def match_stats(match_id, api_key):
    return _get("/matches/%s/stats" % match_id, api_key)


def extract(stats_payload, pid):
    """One row per map in the match for the given player, schema-agnostic.

    The API nests: rounds[] (a map each) -> teams[] -> players[] -> player_stats{}.
    """
    rows = []
    for rnd in stats_payload.get("rounds", []):
        round_stats = rnd.get("round_stats", {}) or {}
        for team in rnd.get("teams", []) or []:
            for player in team.get("players", []) or []:
                if player.get("player_id") != pid:
                    continue
                won = str(round_stats.get("Winner", "")) == str(team.get("team_id", ""))
                rows.append({
                    "match_id": stats_payload.get("match_id") or rnd.get("match_id"),
                    "map": round_stats.get("Map"),
                    "score": round_stats.get("Score"),
                    "won": won,
                    "nickname": player.get("nickname"),
                    "stats": dict(player.get("player_stats", {}) or {}),
                })
    return rows


def collect(nickname, api_key, window_days=21, log=print, seen=()):
    """Stat rows for matches in the window that are not already known."""
    pid = player_id(nickname, api_key)
    matches = history(pid, api_key, window_days)
    log("FACEIT stats: %d match(es) in the last %d days" % (len(matches), window_days))
    rows = []
    for item in matches:
        mid = item.get("match_id")
        if not mid or mid in seen:
            continue
        try:
            payload = match_stats(mid, api_key)
        except Exception as e:
            log("no stats for %s: %r" % (mid, e))
            continue
        for row in extract(payload, pid):
            row["match_id"] = mid
            row["finished_at"] = item.get("finished_at")
            rows.append(row)
    log("FACEIT stats: %d new map row(s)" % len(rows))
    return rows


def as_number(value):
    """Stat values arrive as strings, sometimes with a % or a comma."""
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace("%", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def scale(key, value):
    """Fraction-style rates become percentages; everything else is untouched."""
    n = as_number(value)
    if n is not None and key.lower() in PERCENT_KEYS:
        return n * 100.0
    return n if n is not None else value


def curated(stats):
    """[(label, value)] for the keys we recognise, in a fixed order."""
    lower = {k.lower(): (k, v) for k, v in stats.items()}
    out = []
    for label, candidates in CURATED:
        for key in candidates:
            if key.lower() in lower:
                real_key, value = lower[key.lower()]
                out.append((label, scale(real_key, value)))
                break
    return out


def averages(rows):
    """Mean of every numeric stat across rows, plus maps played and win rate."""
    totals, counts = {}, {}
    for row in rows:
        for key, value in row.get("stats", {}).items():
            n = as_number(value)
            if n is None:
                continue
            totals[key] = totals.get(key, 0.0) + n
            counts[key] = counts.get(key, 0) + 1
    avg = {k: totals[k] / counts[k] for k in totals}
    avg["_maps"] = len(rows)
    if rows:
        avg["_win_rate"] = 100.0 * sum(1 for r in rows if r.get("won")) / len(rows)
    return avg
