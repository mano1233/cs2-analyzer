"""Render the HTML report from parsed results - no network, no demo parsing.

Three levels, and the structure is the point (DEV-60):

  index.html          team overview: what moved, which partnerships to fix, the roster
  player-<name>.html  investigation: the job this player does, and how they do it
  match-<id>.html     one match: every round, the opening duel, the buy, the plant

The old pages grew out of what the data made possible rather than a decision about what
they were for, which is why adding metrics never helped. Each level here answers one
question and hands off to the next.

Two rules the pages keep everywhere:

  * a rate is shown with what it was computed from, and a thin sample is marked. A rate
    over eleven flashes and one over four thousand shots do not deserve equal confidence.
  * a convention (trade range, blind duration, economy thresholds) is stated, never
    presented as a measurement.

Written to REPORT_DIR (a volume the nginx pod serves); safe to call with any number of
matches, including none.
"""
import datetime as dt
import html
import pathlib

import analyze
import artifacts
import faceit_stats
from analyze import ME

# The five numbers a session is judged on. Each maps to something practisable, and each
# names the counter it is computed from so the page can show its own sample size.
HEADLINE = [
    ("moving 1st shot%", "lower", "Stop moving before the first shot", "first_shots", "first shots"),
    ("accuracy%", "higher", "Hit what you shoot at", "shots", "shots"),
    ("util before 1st kill%", "higher", "Utility out before the fight starts", "util_thrown", "thrown"),
    ("flash hit% (>=1 enemy)", "higher", "Flashes that blind somebody", "flashes", "flashes"),
    ("traded death% (of available)", "higher", "Deaths the team punished when it could",
     "deaths_tradeable", "chances"),
]
RECENT = 5              # matches in the "now" window; the rest are "before"
# Match pages are the bulk of the output (~20 KB each) and the whole report is delivered
# in one ConfigMap, which the publisher refuses above ~900 KB. Without a cap the report
# would one day stop updating entirely rather than just losing its oldest match page.
# Aggregates still cover every match; only the per-round pages are limited.
MAX_MATCH_PAGES = 25
THIN_SAMPLE = 60        # below this, a rate is marked rather than trusted to a decimal
SIDE_NAMES = {analyze.T_SIDE: "T", analyze.CT_SIDE: "CT"}

CSS = """
:root{
 --ground:#F3F5F4;--surface:#FFFFFF;--sunk:#EAEEED;
 --ink:#16211F;--muted:#5F716F;--line:#D6DEDC;
 --accent:#0E6F6A;--accent-soft:#D9EAE8;
 --good:#43702F;--good-soft:#DCE9D3;--bad:#A8502A;--bad-soft:#F2DED3;
 --flat:#6B7B79;--spark:#8FA6A3;
 --eco:#9A6B3F;--force:#7A6FA8;--full:#2F6E86;
 --display:"Archivo","Helvetica Neue",Arial,sans-serif;
 --body:"Source Sans 3","Helvetica Neue",Arial,sans-serif;
 --mono:"IBM Plex Mono",ui-monospace,Menlo,monospace;
}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
 --ground:#10171A;--surface:#182126;--sunk:#141C20;
 --ink:#E4EDEB;--muted:#93A7A4;--line:#2A373C;
 --accent:#45B3AC;--accent-soft:#183430;
 --good:#7FB05F;--good-soft:#24351C;--bad:#DD8A57;--bad-soft:#3A241A;
 --flat:#839593;--spark:#6E8785;
 --eco:#C79A6B;--force:#A79BD8;--full:#6FB3CE;
}}
:root[data-theme="dark"]{
 --ground:#10171A;--surface:#182126;--sunk:#141C20;
 --ink:#E4EDEB;--muted:#93A7A4;--line:#2A373C;
 --accent:#45B3AC;--accent-soft:#183430;
 --good:#7FB05F;--good-soft:#24351C;--bad:#DD8A57;--bad-soft:#3A241A;
 --flat:#839593;--spark:#6E8785;
 --eco:#C79A6B;--force:#A79BD8;--full:#6FB3CE;
}
*{box-sizing:border-box}
body{margin:0;background:var(--ground);color:var(--ink);font-family:var(--body);
 font-size:16px;line-height:1.5;-webkit-font-smoothing:antialiased}
.wrap{max-width:1080px;margin:0 auto;padding:34px 24px 72px}
h1,h2,h3,h4{font-family:var(--display);margin:0;text-wrap:balance}
h1{font-size:2.05rem;font-weight:800;letter-spacing:-.022em}
h2{font-size:.95rem;font-weight:700;text-transform:uppercase;letter-spacing:.06em;
 margin:38px 0 4px;padding-bottom:8px;border-bottom:1px solid var(--line)}
h3{font-size:1.9rem;font-weight:800;letter-spacing:-.025em}
h4{font-size:.8rem;font-weight:700}
.crumb{font-family:var(--mono);font-size:.76rem;color:var(--muted);margin:0 0 20px}
.crumb a{color:var(--accent);text-decoration:none}
.crumb a:hover{text-decoration:underline}
.hint{margin:8px 0 14px;color:var(--muted);font-size:.86rem;max-width:68ch}
.lede{color:var(--muted);max-width:64ch;margin:10px 0 0}
.band{display:flex;flex-wrap:wrap;gap:9px 24px;align-items:center;font-family:var(--mono);
 font-size:.79rem;color:var(--muted);border-left:3px solid var(--accent);
 padding:2px 0 2px 13px;margin-top:18px}
.band b{color:var(--ink);font-weight:600}
.role{font-family:var(--mono);font-size:.66rem;text-transform:uppercase;letter-spacing:.08em;
 color:var(--muted);border:1px solid var(--line);border-radius:3px;padding:1px 6px}
.role--big{color:var(--accent);border-color:var(--accent);font-size:.7rem}
.tiles{display:grid;gap:13px;grid-template-columns:repeat(auto-fit,minmax(200px,1fr))}
.tile{background:var(--surface);border:1px solid var(--line);border-radius:6px;
 padding:15px 15px 12px;display:flex;flex-direction:column;gap:6px}
.tile .why{margin:0;font-size:.79rem;color:var(--muted);min-height:2.4em}
.tile__row{display:flex;align-items:baseline;justify-content:space-between;gap:8px}
.big{font-family:var(--mono);font-size:1.75rem;font-weight:600;letter-spacing:-.02em;
 font-variant-numeric:tabular-nums}
.unit{font-size:.9rem;color:var(--muted);margin-left:2px}
.chip{font-family:var(--mono);font-size:.71rem;padding:2px 7px;border-radius:3px;white-space:nowrap}
.chip--good{color:var(--good);background:var(--good-soft)}
.chip--bad{color:var(--bad);background:var(--bad-soft)}
.chip--flat{color:var(--flat);background:var(--sunk)}
.spark{width:100%;height:34px;display:block}
.foot{margin:0;font-family:var(--mono);font-size:.67rem;color:var(--muted)}
.thin{color:var(--bad);border-bottom:1px dotted currentColor}
.pairs{list-style:none;margin:0;padding:0}
.pair{display:grid;grid-template-columns:minmax(200px,1fr) minmax(80px,2fr) auto;gap:15px;
 align-items:center;padding:11px 12px;border-bottom:1px solid var(--line)}
.pair--mine{background:var(--accent-soft);border-radius:4px}
.pair__who{font-size:.92rem}
.arrow{color:var(--muted)}
.pair__bar{background:var(--sunk);height:7px;border-radius:4px;overflow:hidden}
.pair__bar span{display:block;height:100%;background:var(--accent);border-radius:4px}
.pair__num{font-family:var(--mono);font-size:1rem;font-weight:600;text-align:right;
 font-variant-numeric:tabular-nums}
.of{display:block;font-size:.67rem;font-weight:400;color:var(--muted);font-family:var(--mono)}
.roster{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(172px,1fr))}
.card{display:flex;flex-direction:column;gap:3px;text-decoration:none;color:inherit;
 background:var(--surface);border:1px solid var(--line);border-radius:6px;padding:14px}
.card--me{border-color:var(--accent)}
.card:hover,.card:focus-visible{border-color:var(--accent)}
.card__name{font-family:var(--display);font-weight:700;font-size:1.05rem}
.card__stat{font-family:var(--mono);font-size:.85rem}
.card__stat b{font-size:1.1rem;font-weight:600}
.card__sub{font-size:.71rem;color:var(--muted);font-family:var(--mono)}
.kpis{display:grid;gap:10px;grid-template-columns:repeat(auto-fit,minmax(142px,1fr))}
.kpi{background:var(--accent-soft);border-radius:5px;padding:12px 14px;display:flex;
 flex-direction:column;gap:2px}
.kpi__label{font-size:.72rem;color:var(--muted)}
.kpi__value{font-family:var(--mono);font-size:1.35rem;font-weight:600;
 font-variant-numeric:tabular-nums}
.stats{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(180px,1fr))}
.stat{background:var(--surface);border:1px solid var(--line);border-left:3px solid var(--accent);
 border-radius:5px;padding:13px 15px;display:flex;flex-direction:column;gap:2px}
.stat--good{border-left-color:var(--good)}
.stat--bad{border-left-color:var(--bad)}
.stat__label{font-size:.74rem;color:var(--muted)}
.stat__value{font-family:var(--mono);font-size:1.55rem;font-weight:600;
 font-variant-numeric:tabular-nums}
.stat__sub{font-family:var(--mono);font-size:.68rem;color:var(--muted)}
.of2{font-size:.95rem;color:var(--muted)}
.phead{display:flex;justify-content:space-between;align-items:flex-end;gap:20px;
 border-bottom:2px solid var(--ink);padding-bottom:14px}
.sub{margin:6px 0 0;color:var(--muted);font-size:.84rem;font-family:var(--mono)}
.ext{font-family:var(--mono);font-size:.78rem;color:var(--accent);white-space:nowrap}
.two{display:grid;gap:26px;grid-template-columns:repeat(auto-fit,minmax(320px,1fr))}
.ribbon{list-style:none;display:flex;flex-wrap:wrap;gap:4px;margin:0;padding:0}
.rd{position:relative;width:34px;height:44px;border-radius:4px;display:flex;
 align-items:flex-end;justify-content:center;padding-bottom:4px;font-family:var(--mono);
 font-size:.66rem;border:1px solid transparent}
.rd--won{background:var(--good-soft);color:var(--good);border-color:var(--good)}
.rd--lost{background:var(--bad-soft);color:var(--bad);border-color:var(--bad)}
.rd--half{margin-left:18px}
.m{position:absolute;display:block}
.m--open{top:5px;left:50%;transform:translateX(-50%);width:6px;height:6px;border-radius:50%;
 background:currentColor}
.m--plant{top:14px;left:50%;transform:translateX(-50%);width:12px;height:3px;border-radius:2px;
 background:currentColor;opacity:.65}
.legend{font-family:var(--mono);font-size:.7rem;color:var(--muted);display:flex;
 align-items:center;gap:6px;flex-wrap:wrap;margin:12px 0 0}
.legend .m{position:static;transform:none;display:inline-block;background:var(--muted)}
.sw{display:inline-block;width:11px;height:11px;border-radius:3px;border:1px solid}
.sw--won{background:var(--good-soft);border-color:var(--good)}
.sw--lost{background:var(--bad-soft);border-color:var(--bad)}
.scroll{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:.87rem}
th{text-align:left;font-family:var(--mono);font-size:.66rem;letter-spacing:.06em;
 text-transform:uppercase;color:var(--muted);font-weight:500;padding:10px 10px 8px;
 border-bottom:1px solid var(--line);white-space:nowrap}
th.n,td.n{text-align:right}
td{padding:8px 10px;border-bottom:1px solid var(--line);white-space:nowrap}
td.n,td.date,td.res,td.map{font-family:var(--mono);font-variant-numeric:tabular-nums}
td.date{color:var(--muted);font-size:.78rem}
td.map{font-size:.8rem}
.res.win{color:var(--good)}
.res.loss{color:var(--bad)}
td.far{color:var(--bad)}
.empty{color:var(--muted);font-style:italic}
.rounds tr.won td:last-child{color:var(--good)}
.rounds tr.lost td:last-child{color:var(--bad)}
.rounds td.ours{color:var(--good)}
.rounds td.theirs{color:var(--bad)}
.bucket{font-family:var(--mono);font-size:.66rem;text-transform:uppercase;letter-spacing:.05em;
 border-radius:3px;padding:1px 6px;border:1px solid}
.bucket--eco{color:var(--eco);border-color:var(--eco)}
.bucket--force{color:var(--force);border-color:var(--force)}
.bucket--full{color:var(--full);border-color:var(--full)}
.note{background:var(--sunk);border:1px solid var(--line);border-radius:6px;padding:16px 18px}
.note p{margin:0 0 10px;font-size:.87rem;max-width:72ch}
.note p:last-child{margin-bottom:0}
code{font-family:var(--mono);font-size:.85em;background:var(--surface);
 border:1px solid var(--line);border-radius:3px;padding:1px 4px}
a{color:var(--accent)}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
"""

FONTS = ('<link rel="preconnect" href="https://fonts.googleapis.com">'
         '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
         '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
         'family=Archivo:wght@600;700;800&family=IBM+Plex+Mono:wght@400;500;600&'
         'family=Source+Sans+3:wght@400;600&display=swap">')


# ---- small helpers ---------------------------------------------------------

def esc(v):
    return html.escape(str(v), quote=True)


def slug(name):
    """Filename-safe and confined to the output directory: names come from lobbies."""
    cleaned = "".join(c if c.isalnum() or c in "-_" else "-" for c in str(name))
    return "player-%s.html" % (cleaned.strip("-").lower() or "player")


def match_slug(match_id):
    cleaned = "".join(c if c.isalnum() or c in "-_" else "-" for c in str(match_id))
    return "match-%s.html" % (cleaned.strip("-").lower() or "match")


def score_cell(match, won, lost, linkable):
    """The score, linked to the match page when one was rendered.

    Older matches keep their row in every table but lose the link rather than pointing
    at a page that does not exist.
    """
    score = "%d&ndash;%d" % (won, lost)
    mid = match_id_of(match)
    if linkable is None or mid in linkable:
        return '<a href="%s">%s</a>' % (esc(match_slug(mid)), score)
    return score


def link(name):
    return '<a href="%s">%s</a>' % (esc(slug(name)), esc(name))


def num(v, digits=2, dash="&mdash;"):
    """NaN and None are "not measured"; neither is a zero and neither prints as one."""
    if v is None:
        return dash
    if isinstance(v, str):
        return esc(v)
    if v != v:
        return dash
    return ("%%.%df" % digits) % v


def page(title, body, generated, nav=""):
    return ("<title>%s</title>%s<style>%s</style>"
            '<div class="wrap">%s%s'
            '<p class="foot" style="margin-top:40px">Generated %s</p></div>'
            % (esc(title), FONTS, CSS,
               '<p class="crumb">%s</p>' % nav if nav else "", body, esc(generated)))


def spark(values, tone, width=132, height=34):
    """One scale, emphasised endpoint, gaps left as gaps.

    A match with no measurement is not a match scoring zero, so the path breaks rather
    than diving to the floor and inventing a collapse.
    """
    pts = [(i, v) for i, v in enumerate(values) if v is not None and v == v]
    if len(pts) < 2:
        return '<svg class="spark" viewBox="0 0 %d %d" aria-hidden="true"></svg>' % (width, height)
    lo = min(v for _, v in pts)
    hi = max(v for _, v in pts)
    span = (hi - lo) or 1.0
    pad = 4
    n = max(len(values) - 1, 1)

    def xy(i, v):
        return (pad + (width - 2 * pad) * (i / n),
                height - pad - (height - 2 * pad) * ((v - lo) / span))

    segs, cur = [], []
    for i, v in enumerate(values):
        if v is None or v != v:
            if len(cur) > 1:
                segs.append(cur)
            cur = []
            continue
        cur.append(xy(i, v))
    if len(cur) > 1:
        segs.append(cur)
    paths = "".join('<path d="M %s" fill="none" stroke="var(--spark)" stroke-width="1.6" '
                    'stroke-linecap="round" stroke-linejoin="round"/>'
                    % " L ".join("%.1f %.1f" % p for p in s) for s in segs)
    lx, ly = xy(*pts[-1])
    colour = {"good": "var(--good)", "bad": "var(--bad)"}.get(tone, "var(--accent)")
    return ('<svg class="spark" viewBox="0 0 %d %d" role="img" aria-label="recent history">'
            '<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="var(--line)" stroke-width="1"/>'
            '%s<circle cx="%.1f" cy="%.1f" r="2.8" fill="%s"/></svg>'
            % (width, height, pad, height - pad, width - pad, height - pad, paths, lx, ly, colour))


def finished_map(stats):
    return {r.get("match_id"): r.get("finished_at") for r in (stats or [])}


def in_order(matches, stats=()):
    """Oldest first by FACEIT finish time where known, else by object key.

    Order matters here: every trend on every page reads left to right, and "recent"
    has to mean recent in time rather than in whatever order R2 listed the objects.
    """
    when = finished_map(stats)

    def key(m):
        mid = artifacts.match_id_from(m.get("source_key", m.get("demo", "")))
        return (when.get(mid) or 0, str(m.get("source_key", m.get("demo", ""))))
    return sorted(matches, key=key)


def match_id_of(m):
    return artifacts.match_id_from(m.get("source_key", m.get("demo", "")))


def date_of(m, stats=()):
    return artifacts.match_date(m, finished_map(stats).get(match_id_of(m)), None)


def pooled(name, matches):
    return analyze.pool(m["players"][name] for m in matches if name in m.get("players", {}))


def delta(label, direction, now, before):
    """Which way is better is a property of the metric, not of the presentation."""
    if now is None or before is None or now != now or before != before:
        return None, None
    change = now - before
    if abs(change) < 0.05:
        return change, "flat"
    improved = change < 0 if direction == "lower" else change > 0
    return change, "good" if improved else "bad"


def tile(label, why, direction, counters, series, recent, before, counter, unit):
    now = recent.get(label)
    change, tone = delta(label, direction, now, before.get(label) if before else None)
    if tone and tone != "flat":
        arrow = "&#9650;" if change > 0 else "&#9660;"
        chip = '<span class="chip chip--%s">%s %s</span>' % (tone, arrow, num(abs(change), 1))
    else:
        chip = '<span class="chip chip--flat">no change</span>'
    sample = int(counters.get(counter, 0) or 0)
    thin = ' <span class="thin" title="Small sample: read the direction, not the decimal">thin</span>' \
        if sample < THIN_SAMPLE else ""
    return ('<article class="tile"><h4>%s</h4><p class="why">%s</p>'
            '<div class="tile__row"><span class="big">%s<span class="unit">%%</span></span>%s</div>'
            '%s<p class="foot">%d %s%s</p></article>'
            % (esc(label), esc(why), num(now, 1), chip, spark(series, tone or "flat"),
               sample, esc(unit), thin))


def headline_tiles(name, ordered):
    """The five tiles, with each metric's own per-match history behind it."""
    mine = [m for m in ordered if name in m.get("players", {})]
    counters = pooled(name, mine)
    recent = analyze.rates(pooled(name, mine[-RECENT:]))
    before = analyze.rates(pooled(name, mine[:-RECENT])) if len(mine) > RECENT else None
    per_match = [analyze.rates(m["players"][name]) for m in mine]
    return "".join(
        tile(label, why, direction, counters, [r.get(label) for r in per_match],
             recent, before or {}, counter, unit)
        for label, direction, why, counter, unit in HEADLINE), len(mine)


def conventions_note():
    c = analyze
    return ('<div class="note"><p>Conventions, not truths, stated so they can be argued with: '
            'a trade is a kill on the killer within <code>%s s</code>, by a teammate who was '
            'within <code>%s m</code> of them. A flash counts as effective at <code>%s s</code> '
            'of blindness, and a kill only counts as converted if it lands inside that window '
            '&mdash; a pop-flash that bought a free peek a second later scores zero. A first '
            'shot counts as moving above <code>%d u/s</code>. A round is an eco below '
            '<code>$%d</code> of team equipment and a full buy above <code>$%d</code>, read at '
            'freeze end.</p><p>Trade range is a distance check, not a line of sight check: we '
            'have player positions, not map geometry, so a teammate behind a wall counts as in '
            'range.</p></div>'
            % (num(c.TRADE_WINDOW / c.TR, 0), num(c.TRADE_RANGE * c.UNIT_M, 0),
               num(c.EFFECTIVE_BLIND, 1), int(c.MOVING), int(c.ECO_MAX), int(c.FORCE_MAX)))


# ---- level 1: the team overview --------------------------------------------

def build_index(matches, generated, stats=(), linkable=None):
    ordered = in_order(matches, stats)
    names = analyze.roster(ordered)
    roles = analyze.assign_roles({n: analyze.role_signals(pooled(n, ordered)) for n in names})
    owner = ME if ME in names else (names[0] if names else ME)

    tiles, n_mine = headline_tiles(owner, ordered)
    taken = analyze.pair_matrix(ordered, "traded_for", names, over="tradeable", scale=100)
    chances = analyze.pair_matrix(ordered, "tradeable", names)

    pairs = []
    for a in names:
        for b in names:
            if a == b or not chances[a][b] or taken[a][b] is None:
                continue
            pairs.append((taken[a][b], int(chances[a][b]), a, b))
    pairs.sort()
    mine = [p for p in pairs if p[2] == owner]
    others = [p for p in pairs if p[2] != owner][:4]

    def pair_row(p, emphasis):
        value, of, a, b = p
        return ('<li class="pair%s"><div class="pair__who"><strong>%s</strong> '
                '<span class="arrow">trades</span> <strong>%s</strong> '
                '<span class="role">%s</span></div>'
                '<div class="pair__bar"><span style="width:%.1f%%"></span></div>'
                '<div class="pair__num">%s%%<span class="of">of %d</span></div></li>'
                % (" pair--mine" if emphasis else "", esc(a), esc(b), esc(roles[b]),
                   max(2.0, min(100.0, value)), num(value, 0), of))

    roster = "".join(
        '<a class="card%s" href="%s"><span class="role">%s</span>'
        '<span class="card__name">%s</span>'
        '<span class="card__stat"><b>%s</b> ADR</span>'
        '<span class="card__sub">%d rounds &middot; %d matches</span></a>'
        % (" card--me" if n == owner else "", esc(slug(n)), esc(roles[n]), esc(n),
           num(analyze.rates(pooled(n, ordered))["ADR"], 1),
           int(pooled(n, ordered).get("rounds", 0)),
           sum(1 for m in ordered if n in m.get("players", {})))
        for n in names)

    rows = ""
    for m in reversed(ordered):
        if owner not in m.get("players", {}):
            continue
        r = analyze.rates(m["players"][owner])
        won = int(m["players"][owner].get("rounds_won", 0))
        lost = m.get("rounds", 0) - won
        rows += ('<tr><td class="date">%s</td><td class="map">%s</td>'
                 '<td class="res %s">%s</td>'
                 '<td class="n">%s</td><td class="n">%s</td><td class="n">%s</td>'
                 '<td class="n">%s</td></tr>'
                 % (esc(date_of(m, stats)), esc(str(m.get("map", "")).replace("de_", "")),
                    "win" if won > lost else "loss", score_cell(m, won, lost, linkable),
                    num(r["ADR"], 1), num(r["K/D"], 2),
                    num(r["moving 1st shot%"], 0), num(r["accuracy%"], 0)))

    dates = [date_of(m, stats) for m in ordered]
    body = """<header>
<h1>Session debrief</h1>
<p class="lede">Every demo parsed so far. What moved, what to fix, and who to look at.</p>
<div class="band"><span><b>%d</b> matches</span><span><b>%d</b> rounds</span>
<span>%s &rarr; %s</span></div>
</header>
<h2>What moved &middot; %s</h2>
<p class="hint">Last %d matches against the %d before them. Each tile shows what it was
computed from; a thin sample is marked rather than trusted to a decimal.</p>
<div class="tiles">%s</div>
<h2>Fix this first</h2>
<p class="hint">Trades taken as a share of the trades that were on, worst first. A chance
means the other player died while this one was alive and within %s m <em>of the killer</em>
&mdash; standing near the body is not a chance.</p>
<ul class="pairs">%s%s</ul>
<h2>The stack</h2>
<p class="hint">Roles are measured from play, not declared. %s</p>
<div class="roster">%s</div>
<h2>Matches</h2>
<div class="scroll"><table><thead><tr><th>Date</th><th>Map</th><th>Result</th>
<th class="n">ADR</th><th class="n">K/D</th><th class="n">Moving 1st%%</th>
<th class="n">Acc%%</th></tr></thead><tbody>%s</tbody></table></div>
<h2>What these numbers assume</h2>
%s""" % (len(ordered), int(pooled(owner, ordered).get("rounds", 0)),
         esc(dates[0] if dates else "?"), esc(dates[-1] if dates else "?"),
         esc(owner), RECENT, max(n_mine - RECENT, 0), tiles,
         num(analyze.TRADE_RANGE * analyze.UNIT_M, 0),
         "".join(pair_row(p, True) for p in mine),
         "".join(pair_row(p, False) for p in others),
         esc(analyze.ROLE_RULE), roster, rows, conventions_note())
    nav = '<a href="faceit.html">FACEIT stats &rarr;</a>' if stats else ""
    return page("Session debrief", body, generated, nav=nav)


# ---- level 2: the player investigation --------------------------------------

def build_player(matches, name, names, roles, generated, stats=(), linkable=None):
    ordered = in_order(matches, stats)
    mine = [m for m in ordered if name in m.get("players", {})]
    counters = pooled(name, ordered)
    all_rates = analyze.rates(counters)
    sig = analyze.role_signals(counters)
    role = roles[name]
    tiles, n_mine = headline_tiles(name, ordered)

    kpis = "".join('<div class="kpi"><span class="kpi__label">%s</span>'
                   '<span class="kpi__value">%s</span></div>'
                   % (esc(k), num(all_rates[k], 2 if isinstance(all_rates[k], float)
                                  and abs(all_rates[k]) < 10 else 1))
                   for k in analyze.ROLE_KPIS.get(role, []) if k in all_rates)

    def scope_rows(pairs):
        out = ""
        for tag, prefix in pairs:
            r = analyze.scope_rates(counters, prefix)
            if not r["rounds"]:
                continue
            out += ('<tr><td>%s</td><td class="n">%d</td><td class="n">%s</td>'
                    '<td class="n">%s</td><td class="n">%s</td><td class="n">%s</td>'
                    '<td class="n">%s</td></tr>'
                    % (esc(tag), r["rounds"], num(r["ADR"], 1), num(r["K/D"], 2),
                       num(r["moving 1st shot%"], 0), num(r["util thrown/r"], 2),
                       esc(r["open W/L"])))
        return out

    taken = analyze.pair_matrix(ordered, "traded_for", names, over="tradeable", scale=100)
    chances = analyze.pair_matrix(ordered, "tradeable", names)
    prox = analyze.pair_matrix(ordered, "prox_sum", names, mean=True)
    apart = analyze.pair_matrix(ordered, "death_dist_sum", names,
                                over="death_dist_n", scale=analyze.UNIT_M)
    range_m = analyze.TRADE_RANGE * analyze.UNIT_M
    partners = ""
    for other in sorted(names, key=lambda o: (taken[name][o] is None, taken[name][o] or 0)):
        if other == name:
            continue
        far = ' class="n far"' if prox[name][other] and prox[name][other] > range_m else ' class="n"'
        partners += ('<tr><td><strong>%s</strong> <span class="role">%s</span></td>'
                     '<td class="n">%s%%<span class="of">of %d</span></td>'
                     '<td class="n">%s%%<span class="of">of %d</span></td>'
                     '<td%s>%s m</td><td class="n">%s m</td></tr>'
                     % (link(other), esc(roles[other]),
                        num(taken[name][other], 0), int(chances[name][other] or 0),
                        num(taken[other][name], 0), int(chances[other][name] or 0),
                        far, num(prox[name][other], 1), num(apart[name][other], 1)))

    rows = ""
    for m in reversed(mine):
        r = analyze.rates(m["players"][name])
        won = int(m["players"][name].get("rounds_won", 0))
        lost = m.get("rounds", 0) - won
        rows += ('<tr><td class="date">%s</td><td class="map">%s</td>'
                 '<td class="res %s">%s</td>'
                 '<td class="n">%s</td><td class="n">%s</td><td class="n">%s</td>'
                 '<td class="n">%s</td></tr>'
                 % (esc(date_of(m, stats)), esc(str(m.get("map", "")).replace("de_", "")),
                    "win" if won > lost else "loss", score_cell(m, won, lost, linkable),
                    num(r["ADR"], 1), num(r["K/D"], 2),
                    num(r["moving 1st shot%"], 0), num(r["accuracy%"], 0)))

    body = """<header class="phead"><div>
<span class="role role--big">%s</span>
<h1>%s</h1>
<p class="sub">%d matches &middot; %d rounds &middot; first contact %s/round &middot;
utility %s/round &middot; survival %s%%</p>
</div></header>
<h2>The job</h2>
<p class="hint">Measured against what a %s is for, not against a league table.</p>
<div class="kpis">%s</div>
<h2>What moved</h2>
<p class="hint">Last %d matches against the %d before them.</p>
<div class="tiles">%s</div>
<div class="two">
<div><h2>By side</h2><div class="scroll"><table><thead><tr><th>Side</th><th class="n">Rds</th>
<th class="n">ADR</th><th class="n">K/D</th><th class="n">Mov%%</th><th class="n">Util/r</th>
<th class="n">1st duels</th></tr></thead><tbody>%s</tbody></table></div></div>
<div><h2>By half</h2><div class="scroll"><table><thead><tr><th>Scope</th><th class="n">Rds</th>
<th class="n">ADR</th><th class="n">K/D</th><th class="n">Mov%%</th><th class="n">Util/r</th>
<th class="n">1st duels</th></tr></thead><tbody>%s</tbody></table></div></div>
</div>
<h2>Partnerships</h2>
<p class="hint">Both directions, each against the chances that existed. A distance above
%s m is marked: that is the trade range, so a pair living beyond it cannot trade whatever
either of them intends.</p>
<div class="scroll"><table><thead><tr><th>Teammate</th><th class="n">%s trades them</th>
<th class="n">They trade %s</th><th class="n">Apart, alive</th>
<th class="n">Apart at death</th></tr></thead><tbody>%s</tbody></table></div>
<h2>Match by match</h2>
<div class="scroll"><table><thead><tr><th>Date</th><th>Map</th><th>Result</th>
<th class="n">ADR</th><th class="n">K/D</th><th class="n">Moving 1st%%</th>
<th class="n">Acc%%</th></tr></thead><tbody>%s</tbody></table></div>
""" % (esc(role), esc(name), len(mine), int(all_rates["rounds"]),
       num(sig["first_contact/r"], 2), num(sig["util/r"], 2), num(sig["survival%"], 0),
       esc(role), kpis, RECENT, max(n_mine - RECENT, 0), tiles,
       scope_rows((("T", "t_"), ("CT", "ct_"))),
       scope_rows((("1st half", "h1_"), ("2nd half", "h2_"), ("Overtime", "ot_"))),
       num(range_m, 0), esc(name), esc(name), partners, rows)
    return page(name, body, generated, nav='<a href="index.html">&larr; Session debrief</a>')


# ---- level 3: one match -----------------------------------------------------

def our_rounds(match):
    """Every round from our side's point of view.

    Sides swap at the half, so a page that prints "T eco win%" makes the reader do that
    translation themselves. These rows are already ours-or-theirs.
    """
    stack = set(analyze.tracked_in(match))
    started = match.get("started_side", {})
    ours = next((started[n] for n in stack if n in started), analyze.T_SIDE)
    out = []
    for r in match.get("round_table", []):
        mine = ours if r.get("half") == 1 else (analyze.T_SIDE + analyze.CT_SIDE - ours)
        tag = "t" if mine == analyze.T_SIDE else "ct"
        out.append({
            "round": r.get("round"), "half": r.get("half"),
            "won": r.get("winner") == mine,
            "side": SIDE_NAMES.get(mine, "?"),
            "opened_for_us": (None if r.get("opening_kill_team") is None
                              else r.get("opening_kill_team") == mine),
            "opener": r.get("opening_killer"), "victim": r.get("opening_victim"),
            "opener_ours": (r.get("opening_killer") in stack) if r.get("opening_killer") else None,
            "time": r.get("opening_time"),
            "planted": bool(r.get("planted")), "defused": bool(r.get("defused")),
            "bucket": r.get("%s_bucket" % tag) or "full",
        })
    return out, SIDE_NAMES.get(ours, "?")


def _pct(part, whole):
    return None if not whole else 100.0 * part / whole


def build_match(match, generated, stats=()):
    rounds, first_side = our_rounds(match)
    mid = match_id_of(match)
    stack = sorted(n for n in analyze.tracked_in(match) if n in match.get("players", {}))
    won = sum(1 for r in rounds if r["won"])
    lost = len(rounds) - won

    cells = ""
    for r in rounds:
        marks = ('<i class="m m--open" title="opening kill ours"></i>' if r["opened_for_us"] else "")
        marks += ('<i class="m m--plant" title="bomb planted"></i>' if r["planted"] else "")
        cells += ('<li class="rd rd--%s%s" title="Round %s, %s, %s"><span>%s</span>%s</li>'
                  % ("won" if r["won"] else "lost", " rd--half" if r["round"] == 13 else "",
                     esc(r["round"]), esc(r["side"]), "won" if r["won"] else "lost",
                     esc(r["round"]), marks))

    got = [r for r in rounds if r["opened_for_us"]]
    lost_open = [r for r in rounds if r["opened_for_us"] is False]
    got_pct = _pct(sum(1 for r in got if r["won"]), len(got))
    save_pct = _pct(sum(1 for r in lost_open if r["won"]), len(lost_open))
    planted = [r for r in rounds if r["planted"]]

    def stat(label, value, sub, tone=""):
        return ('<div class="stat%s"><span class="stat__label">%s</span>'
                '<span class="stat__value">%s</span><span class="stat__sub">%s</span></div>'
                % (tone, esc(label), value, esc(sub)))

    cards = stat("Opening duels won", "%d<span class='of2'>/%d</span>" % (len(got), len(rounds)),
                 "of %d rounds" % len(rounds))
    cards += stat("Converted after opening kill", num(got_pct, 0) + "%",
                  "%d rounds" % len(got), " stat--good" if (got_pct or 0) >= 60 else "")
    cards += stat("Saved after opening death", num(save_pct, 0) + "%",
                  "%d rounds" % len(lost_open), " stat--bad" if (save_pct or 0) < 25 else "")
    cards += stat("Rounds with a plant", str(len(planted)),
                  "%s%% of rounds" % num(_pct(len(planted), len(rounds)), 0))

    buys = ""
    for b in analyze.BUCKETS:
        rs = [r for r in rounds if r["bucket"] == b]
        buys += ('<tr><td><span class="bucket bucket--%s">%s</span></td><td class="n">%d</td>'
                 '<td class="n">%s%%</td></tr>'
                 % (b, b, len(rs), num(_pct(sum(1 for r in rs if r["won"]), len(rs)), 0)))

    sites = ""
    for _, clusters in (analyze.plant_sites([match]) or {}).items():
        for i, c in enumerate(clusters, 1):
            sites += ('<tr><td>Site %d</td><td class="n">%d</td><td class="n">%s%%</td>'
                      '<td class="n">%s%%</td></tr>'
                      % (i, c["plants"], num(c["t_win%"], 0), num(c["defused%"], 0)))
    sites = sites or '<tr><td colspan="4" class="empty">No plants in this match.</td></tr>'

    table_rows = ""
    for r in rounds:
        table_rows += ('<tr class="%s"><td class="n">%s</td><td>%s</td>'
                       '<td><span class="bucket bucket--%s">%s</span></td>'
                       '<td class="%s">%s</td><td>%s</td><td>%s</td><td class="n">%s</td>'
                       '<td>%s</td></tr>'
                       % ("won" if r["won"] else "lost", esc(r["round"]), esc(r["side"]),
                          r["bucket"], r["bucket"],
                          "ours" if r["opener_ours"] else "theirs",
                          esc(r["opener"] or "—"), esc(r["victim"] or "—"),
                          "planted" if r["planted"] else ("defused" if r["defused"] else ""),
                          num(r["time"], 1), "won" if r["won"] else "lost"))

    board = ""
    for n in sorted(stack, key=lambda n: -(analyze.rates(match["players"][n])["ADR"] or 0)):
        r = analyze.rates(match["players"][n])
        board += ('<tr><td>%s</td><td class="n">%s</td><td class="n">%s</td><td class="n">%s</td>'
                  '<td class="n">%s</td><td class="n">%s</td><td class="n">%s</td></tr>'
                  % (link(n), num(r["ADR"], 1), num(r["K/D"], 2), num(r["HS%"], 0),
                     num(r["accuracy%"], 0), num(r["moving 1st shot%"], 0),
                     num(r["util thrown/r"], 2)))

    room = artifacts.FACEIT_ROOM % mid if artifacts.source_of(mid) == "faceit" else None
    body = """<header class="phead"><div>
<span class="role role--big">%s &middot; %s</span>
<h3>%d &ndash; %d</h3>
<p class="sub">%d rounds &middot; started on %s</p>
</div>%s</header>
<h2>How the match went</h2>
<p class="hint">One cell per round, ours to lose. A dot means we took the opening kill;
a bar means the bomb went down. The gap is the half.</p>
<ol class="ribbon">%s</ol>
<p class="legend"><i class="m m--open"></i> opening kill ours &nbsp;
<i class="m m--plant"></i> bomb planted &nbsp;
<span class="sw sw--won"></span> won &nbsp;<span class="sw sw--lost"></span> lost</p>
<h2>Where the rounds went</h2>
<div class="stats">%s</div>
<div class="two">
<div><h2>Buy</h2><p class="hint">Our equipment at freeze end.</p>
<div class="scroll"><table><thead><tr><th>Buy</th><th class="n">Rounds</th>
<th class="n">Won</th></tr></thead><tbody>%s</tbody></table></div></div>
<div><h2>Plant sites</h2><p class="hint">Clustered from bomb coordinates, so these are
positions rather than the demo's own site ids.</p>
<div class="scroll"><table><thead><tr><th>Site</th><th class="n">Plants</th>
<th class="n">T win</th><th class="n">Defused</th></tr></thead><tbody>%s</tbody></table></div></div>
</div>
<h2>Round by round</h2>
<div class="scroll"><table class="rounds"><thead><tr><th class="n">#</th><th>Side</th>
<th>Buy</th><th>First blood</th><th>On</th><th>Bomb</th><th class="n">Time</th>
<th>Result</th></tr></thead><tbody>%s</tbody></table></div>
<h2>Scoreboard</h2>
<div class="scroll"><table><thead><tr><th>Player</th><th class="n">ADR</th><th class="n">K/D</th>
<th class="n">HS%%</th><th class="n">Acc%%</th><th class="n">Moving 1st%%</th>
<th class="n">Util/r</th></tr></thead><tbody>%s</tbody></table></div>
""" % (esc(str(match.get("map", "")).replace("de_", "")), esc(date_of(match, stats)),
       won, lost, len(rounds), esc(first_side),
       ('<a class="ext" href="%s" rel="noreferrer">FACEIT room &rarr;</a>' % esc(room)) if room else "",
       cells, cards, buys, sites, table_rows, board)
    return page("%s %d-%d" % (str(match.get("map", "")).replace("de_", ""), won, lost),
                body, generated, nav='<a href="index.html">&larr; Session debrief</a>')


# ---- the FACEIT appendix ----------------------------------------------------

def curated_averages(avg):
    """[(label, value)] for recognised stats, tolerating the API's shifting key names."""
    lower = {k.lower(): (k, v) for k, v in avg.items()}
    out = []
    for label, candidates in faceit_stats.CURATED:
        for key in candidates:
            if key.lower() in lower:
                real_key, value = lower[key.lower()]
                out.append((label, faceit_stats.scale(real_key, value)))
                break
    return out


def build_faceit(stats, generated, has_demos):
    avg = faceit_stats.averages(stats)
    cards = "".join('<div class="kpi"><span class="kpi__label">%s</span>'
                    '<span class="kpi__value">%s</span></div>' % (esc(l), num(v, 1))
                    for l, v in curated_averages(avg) if l in faceit_stats.HEADLINE)
    labels = [l for l, _ in faceit_stats.CURATED]
    head = "".join('<th class="n">%s</th>' % esc(l) for l in labels)
    rows = ""
    for row in sorted(stats, key=lambda r: r.get("finished_at") or 0, reverse=True):
        cells = dict(curated_averages(row.get("stats", {})))
        rows += ('<tr><td class="date">%s</td><td class="map">%s</td>%s</tr>'
                 % (esc(artifacts.match_date({}, row.get("finished_at"), None)),
                    esc(str(row.get("map", "")).replace("de_", "")),
                    "".join('<td class="n">%s</td>' % num(cells.get(l), 1) for l in labels)))
    nav = '<a href="index.html">&larr; Session debrief</a>' if has_demos else ""
    body = """<header><h1>Recent form</h1>
<p class="lede">Straight from FACEIT for every match in the window. These are the
scoreboard and FACEIT's own advanced stats; the demo-only findings live in the debrief.</p></header>
<h2>Averages</h2><div class="kpis">%s</div>
<h2>Match by match</h2>
<div class="scroll"><table><thead><tr><th>Date</th><th>Map</th>%s</tr></thead>
<tbody>%s</tbody></table></div>
<p class="hint">Blank cells are stats FACEIT did not return for that match. Nothing here is
merged with the demo numbers: FACEIT's "flash success" and our "flash hit" measure
different things, and averaging them produces a number nobody can explain.</p>
""" % (cards, head, rows)
    return page("Recent form", body, generated, nav=nav)


# ---- entry point ------------------------------------------------------------

def render(matches, out_dir, stats=()):
    out = pathlib.Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    generated = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    playable = [m for m in matches if ME in m.get("players", {})]
    stats = list(stats)

    if not playable:
        # No demo parsed yet: the API stats still make a useful front page.
        if stats:
            body = build_faceit(stats, generated, has_demos=False)
        else:
            body = page("Session debrief", '<header><h1>No matches yet</h1>'
                        '<p class="lede">No demo has been parsed and the FACEIT stats sync '
                        'has returned nothing yet.</p></header>', generated)
        (out / "index.html").write_text(body, encoding="utf-8")
        return [out / "index.html"]

    names = analyze.roster(playable)
    roles = analyze.assign_roles({n: analyze.role_signals(pooled(n, playable)) for n in names})

    # Newest matches keep their per-round page; older ones stay in every aggregate and
    # simply stop being clickable.
    recent_first = list(reversed(in_order(playable, stats)))
    with_rounds = [m for m in recent_first if m.get("round_table")][:MAX_MATCH_PAGES]
    linkable = {match_id_of(m) for m in with_rounds}

    pages = {"index.html": build_index(playable, generated, stats, linkable)}
    if stats:
        pages["faceit.html"] = build_faceit(stats, generated, has_demos=True)
    for n in names:
        pages[slug(n)] = build_player(playable, n, names, roles, generated, stats, linkable)
    for m in with_rounds:
        pages[match_slug(match_id_of(m))] = build_match(m, generated, stats)

    written = []
    for name, content in pages.items():
        (out / name).write_text(content, encoding="utf-8")
        written.append(out / name)
    return written


if __name__ == "__main__":
    import json
    import sys
    data = json.loads(pathlib.Path("results.json").read_text())
    for f in render(data, sys.argv[1] if len(sys.argv) > 1 else "report"):
        print("wrote", f)
