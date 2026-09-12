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

# Curated view: label -> candidate keys, tried in order, case-insensitively. FACEIT has
# shipped both "Flash Successes" and "Flash Success Rate" style names.
CURATED = [
    ("Kills", ["Kills"]),
    ("Deaths", ["Deaths"]),
    ("Assists", ["Assists"]),
    ("K/D", ["K/D Ratio", "K/D"]),
    ("K/R", ["K/R Ratio", "K/R"]),
    ("ADR", ["ADR", "Average Damage per Round", "Damage per Round"]),
    ("HS %", ["Headshots %", "Headshot %", "Headshots percentage"]),
    ("MVPs", ["MVPs"]),
    ("Utility damage", ["Utility Damage", "Utility Damage per Round"]),
    ("Enemies flashed", ["Enemies Flashed", "Enemies Flashed per Round"]),
    ("Flash success", ["Flash Success Rate", "Flash Successes", "Flash Success Rate per Match"]),
    ("Entries", ["Entry Count", "Entry Rate"]),
    ("Entry wins", ["Entry Wins", "Entry Success Rate"]),
    ("Clutches", ["Clutch Kills", "1v1Wins", "1v2Wins", "Match 1v1 Wins"]),
]


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


def curated(stats):
    """[(label, raw value)] for the keys we recognise, in a fixed order."""
    lower = {k.lower(): v for k, v in stats.items()}
    out = []
    for label, candidates in CURATED:
        for key in candidates:
            if key.lower() in lower:
                out.append((label, lower[key.lower()]))
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
