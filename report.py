"""Render the HTML report from parsed results - no network, no demo parsing.

Two pages, regenerated on every run so they always show the current sample:
  index.html  headline metrics against targets, plus one row per match
  team.html   roster with measured roles, per-half splits, trade and distance matrices

Written to REPORT_DIR (a volume the nginx pod serves); safe to call with any number of
matches, including none.
"""
import datetime as dt
import html
import pathlib

import analyze
import faceit_stats
from analyze import ME

# What "good" looks like, from the lobby averages of the first sample. Kept here rather
# than in the page text so the targets move in one place.
TARGETS = {
    "moving 1st shot%": ("<", 45.0),
    "accuracy%": (">", 18.0),
    "ADR": (">", 80.0),
    "util thrown/r": (">", 1.30),
    "flash hit% (>=1 enemy)": (">", 25.0),
    "K/D": (">", 1.0),
}

CSS = """
:root{--ground:#eef1f4;--surface:#fff;--surface-2:#f5f8fa;--ink:#121c24;--ink-2:#3a4a56;
--muted:#63757f;--line:#d1dae1;--line-2:#e4eaee;--accent:#1f6480;--accent-ink:#175367;
--accent-wash:#dcebf2;--good:#2b6f52;--bad:#a43a34;--warn:#8a6410;
--shadow:0 1px 2px rgba(18,28,36,.06),0 8px 22px rgba(18,28,36,.05)}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--ground:#0d1317;
--surface:#141d23;--surface-2:#19242b;--ink:#e6eef3;--ink-2:#bac8d1;--muted:#8b9ea9;
--line:#243139;--line-2:#1e2a31;--accent:#59a8c8;--accent-ink:#7cc0dc;--accent-wash:#132630;
--good:#5cbf8e;--bad:#e0706a;--warn:#d6ac52;--shadow:0 1px 2px rgba(0,0,0,.4),0 10px 26px rgba(0,0,0,.3)}}
:root[data-theme="dark"]{--ground:#0d1317;--surface:#141d23;--surface-2:#19242b;--ink:#e6eef3;
--ink-2:#bac8d1;--muted:#8b9ea9;--line:#243139;--line-2:#1e2a31;--accent:#59a8c8;
--accent-ink:#7cc0dc;--accent-wash:#132630;--good:#5cbf8e;--bad:#e0706a;--warn:#d6ac52;
--shadow:0 1px 2px rgba(0,0,0,.4),0 10px 26px rgba(0,0,0,.3)}
*{box-sizing:border-box}
body{background:var(--ground);color:var(--ink);font-family:"Source Sans 3",system-ui,sans-serif;
font-size:17px;line-height:1.6;padding:30px 20px 64px;margin:0}
.wrap{max-width:940px;margin:0 auto;display:flex;flex-direction:column;gap:38px}
h1,h2{font-family:Archivo,system-ui,sans-serif;margin:0;letter-spacing:-.015em;text-wrap:balance}
h1{font-size:clamp(1.9rem,4.6vw,2.6rem);font-weight:700;line-height:1.07}
h2{font-size:1.32rem;font-weight:700}
p{margin:0}
a{color:var(--accent-ink)}
section{display:flex;flex-direction:column;gap:14px}
.eyebrow{font-family:"JetBrains Mono",monospace;font-size:.72rem;letter-spacing:.14em;
text-transform:uppercase;color:var(--muted)}
.masthead{display:flex;flex-direction:column;gap:12px;border-bottom:2px solid var(--ink);padding-bottom:22px}
.note{font-size:.9rem;color:var(--muted);max-width:70ch}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:13px}
.card{background:var(--surface);border:1px solid var(--line);border-radius:4px;padding:15px 17px;
box-shadow:var(--shadow);display:flex;flex-direction:column;gap:5px}
.card .label{font-size:.76rem;text-transform:uppercase;letter-spacing:.05em;color:var(--muted)}
.card .value{font-family:"JetBrains Mono",monospace;font-size:1.75rem;font-weight:600;
font-variant-numeric:tabular-nums;line-height:1.1}
.card .value.good{color:var(--good)}.card .value.bad{color:var(--bad)}
.card .target{font-family:"JetBrains Mono",monospace;font-size:.76rem;color:var(--muted)}
.tablewrap{overflow-x:auto;border:1px solid var(--line);border-radius:4px;background:var(--surface)}
table{border-collapse:collapse;width:100%;font-size:.9rem;font-variant-numeric:tabular-nums;min-width:560px}
th,td{text-align:right;padding:9px 12px;border-bottom:1px solid var(--line-2);white-space:nowrap}
th:first-child,td:first-child{text-align:left}
thead th{font-family:Archivo,sans-serif;font-size:.74rem;text-transform:uppercase;
letter-spacing:.07em;color:var(--muted);background:var(--surface-2)}
tbody tr:last-child td{border-bottom:0}
tbody th{font-family:Archivo,sans-serif;font-weight:600;text-align:left;background:var(--surface-2)}
td.me,th.me{color:var(--accent-ink);font-weight:600}
td.dim{color:var(--muted)}
.role{font-family:"JetBrains Mono",monospace;font-size:.7rem;text-transform:uppercase;
letter-spacing:.07em;color:var(--accent-ink);background:var(--accent-wash);border-radius:2px;padding:2px 6px}
footer{border-top:1px solid var(--line);padding-top:16px;color:var(--muted);font-size:.85rem;
display:flex;flex-direction:column;gap:6px}
"""


def esc(v):
    return html.escape(str(v))


def slug(name):
    """Filename for a player page. Names come from demos, so keep only safe chars."""
    keep = "".join(c if c.isalnum() or c in "-_" else "-" for c in name)
    return "player-%s.html" % keep.strip("-").lower()


def link(name):
    return f'<a href="{esc(slug(name))}">{esc(name)}</a>'


def num(v, digits=2, dash="-"):
    if v is None:
        return dash
    if isinstance(v, str):
        return esc(v)
    if v != v:  # NaN
        return dash
    return f"{v:.{digits}f}"


def page(title, body, generated, nav=""):
    return f"""<title>{esc(title)}</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Archivo:wght@600;700&family=JetBrains+Mono:wght@400;600&family=Source+Sans+3:wght@400;600&display=swap">
<style>{CSS}</style>
<div class="wrap">
{body}
<footer>
<p>Generated {esc(generated)} by the cs2-analyzer CronJob. {nav}</p>
<p>Small samples: single-match swings are noise; trends across matches are not.</p>
</footer>
</div>
"""


def table(headers, rows, first_col_header=True):
    head = "".join(f"<th>{esc(h)}</th>" for h in headers)
    body = []
    for row in rows:
        cells = []
        for i, c in enumerate(row):
            cls = ' class="me"' if isinstance(c, str) and c == ME else ""
            tag = "th" if i == 0 and first_col_header else "td"
            cells.append(f"<{tag}{cls}>{c if isinstance(c, str) and c.startswith('<') else esc(c)}</{tag}>")
        body.append("<tr>" + "".join(cells) + "</tr>")
    return ('<div class="tablewrap"><table><thead><tr>' + head + "</tr></thead><tbody>"
            + "".join(body) + "</tbody></table></div>")


def metric_cards(r):
    cards = []
    for key, (direction, target) in TARGETS.items():
        v = r.get(key)
        if v is None or (isinstance(v, float) and v != v):
            continue
        ok = v < target if direction == "<" else v > target
        digits = 2 if key in ("K/D", "util thrown/r") else 1
        cards.append(
            f'<div class="card"><span class="label">{esc(key)}</span>'
            f'<span class="value {"good" if ok else "bad"}">{num(v, digits)}</span>'
            f'<span class="target">target {direction} {num(target, digits)}</span></div>')
    return '<div class="cards">' + "".join(cards) + "</div>"


def build_index(matches, generated):
    me_pooled = analyze.pool(m["players"][ME] for m in matches if ME in m["players"])
    r = analyze.rates(me_pooled)
    rounds = int(me_pooled["rounds"])

    rows = []
    for m in sorted(matches, key=lambda m: m.get("source_key", m["demo"])):
        me = m["players"].get(ME)
        if not me:
            continue
        mr = analyze.rates(me)
        won = int(me.get("rounds_won", 0))
        rows.append([
            m["map"].replace("de_", ""),
            f'{won}-{m["rounds"] - won}',
            "premier" if "match730" in m.get("source_key", m["demo"]) else "faceit",
            num(mr["moving 1st shot%"], 1),
            num(mr["accuracy%"], 1),
            num(mr["ADR"], 1),
            num(mr["util thrown/r"]),
            num(mr["flash hit% (>=1 enemy)"], 1),
        ])

    body = f"""<header class="masthead">
<p class="eyebrow">{esc(ME)} &middot; {len(matches)} matches &middot; {rounds} rounds</p>
<h1>Where the rounds go</h1>
<p class="note">Regenerated from every demo the analyzer has parsed. Green means the target is met; the target itself is the lobby average that beat you.</p>
</header>
<section>
<h2>Now</h2>
{metric_cards(r)}
<p class="note">Moving first shot is the one to watch: it drags accuracy, damage and opening duels together.</p>
</section>
<section>
<h2>Match by match</h2>
{table(["Map", "Result", "Source", "Moving 1st %", "Acc %", "ADR", "Util/r", "Flash hit %"], rows)}
</section>"""
    return page("Where the rounds go", body, generated,
                nav='<a href="team.html">Team report</a> &middot; <a href="faceit.html">Recent form (FACEIT)</a>')


def build_team(matches, generated):
    names = analyze.roster(matches)
    pooled = analyze.pooled_players(matches, names)
    sig = {n: analyze.role_signals(pooled[n]) for n in names}
    roles = analyze.assign_roles(sig)

    indiv = []
    for n in names:
        r = analyze.rates(pooled[n])
        indiv.append([link(n), f'<span class="role">{esc(roles[n])}</span>', int(r["rounds"]),
                      num(r["K/D"]), num(r["ADR"], 1), num(r["HS%"], 1), num(r["accuracy%"], 1),
                      num(r["moving 1st shot%"], 1), num(r["util thrown/r"]), r["open W/L"]])

    halves = []
    for n in names:
        h1 = analyze.scope_rates(pooled[n], "h1_")
        h2 = analyze.scope_rates(pooled[n], "h2_")
        delta = h2["ADR"] - h1["ADR"] if h1["rounds"] and h2["rounds"] else float("nan")
        halves.append([link(n), h1["rounds"], num(h1["ADR"], 1), num(h2["ADR"], 1), num(delta, 1),
                       num(h1["moving 1st shot%"], 1), num(h2["moving 1st shot%"], 1),
                       num(h1["util thrown/r"]), num(h2["util thrown/r"])])

    def matrix_table(key, mean, digits):
        m = analyze.pair_matrix(matches, key, names, mean=mean)
        rows = [[link(a)] + [num(m[a][b], digits, dash="&mdash;") for b in names] for a in names]
        return table([""] + names, rows)

    body = f"""<header class="masthead">
<p class="eyebrow">stack report &middot; {len(matches)} matches</p>
<h1>Who trades whom</h1>
<p class="note">Roles are measured, not claimed: {esc(analyze.ROLE_RULE)}</p>
</header>
<section>
<h2>Individual</h2>
{table(["Player", "Role", "Rounds", "K/D", "ADR", "HS%", "Acc%", "Moving 1st%", "Util/r", "1st duels"], indiv)}
</section>
<section>
<h2>Per half</h2>
{table(["Player", "H1 rounds", "ADR H1", "ADR H2", "ADR change", "Moving H1%", "Moving H2%", "Util/r H1", "Util/r H2"], halves)}
<p class="note">MR12: rounds 1-12, 13-24, then overtime. Discipline slipping in second halves shows up here before it shows up in the scoreline.</p>
</section>
<section>
<h2>Trades: row killed the enemy who had just killed column</h2>
{matrix_table("traded_for", False, 0)}
</section>
<section>
<h2>Flash conversions: row's flash, column got the kill</h2>
{matrix_table("flash_conv", False, 0)}
</section>
<section>
<h2>Average distance while both alive (metres)</h2>
{matrix_table("prox_sum", True, 1)}
<p class="note">Trades need roughly 10-15 m and a shared corridor. Beyond about 20 m you arrive after the fight is decided.</p>
</section>"""
    return page("Who trades whom", body, generated, nav='<a href="index.html">&larr; Personal report</a>')


def build_player(matches, name, names, roles, generated):
    pooled = analyze.pool(m["players"][name] for m in matches if name in m["players"])
    r = analyze.rates(pooled)
    sig = analyze.role_signals(pooled)
    role = roles[name]
    played = [m for m in matches if name in m["players"]]

    # Role-specific first: an entry and a support fail in different ways.
    kpis = [(k, r[k]) for k in analyze.ROLE_KPIS[role] if k in r]
    kpi_cards = "".join(
        f'<div class="card"><span class="label">{esc(k)}</span>'
        f'<span class="value">{num(v, 2 if isinstance(v, float) and abs(v) < 10 else 1)}</span></div>'
        for k, v in kpis)

    sides = [["T", int(pooled["t_rounds"]),
              num(analyze.scope_rates(pooled, "t_")["ADR"], 1),
              analyze.scope_rates(pooled, "t_")["open W/L"],
              num(analyze.scope_rates(pooled, "t_")["util thrown/r"])],
             ["CT", int(pooled["ct_rounds"]),
              num(analyze.scope_rates(pooled, "ct_")["ADR"], 1),
              analyze.scope_rates(pooled, "ct_")["open W/L"],
              num(analyze.scope_rates(pooled, "ct_")["util thrown/r"])]]

    halves = []
    for label, prefix in (("1st half", "h1_"), ("2nd half", "h2_"), ("Overtime", "ot_")):
        h = analyze.scope_rates(pooled, prefix)
        if not h["rounds"]:
            continue
        halves.append([label, h["rounds"], num(h["ADR"], 1), num(h["K/D"]),
                       num(h["moving 1st shot%"], 1), num(h["util thrown/r"]), h["open W/L"]])

    trades = analyze.pair_matrix(matches, "traded_for", names)
    flashes = analyze.pair_matrix(matches, "flash_conv", names)
    prox = analyze.pair_matrix(matches, "prox_sum", names, mean=True)
    pairs = []
    for other in names:
        if other == name:
            continue
        pairs.append([link(other), num(trades[name][other], 0), num(trades[other][name], 0),
                      num(flashes[name][other], 0), num(flashes[other][name], 0),
                      num(prox[name][other], 1)])

    per_match = []
    for m in sorted(played, key=lambda m: m.get("source_key", m["demo"])):
        mr = analyze.rates(m["players"][name])
        won = int(m["players"][name].get("rounds_won", 0))
        per_match.append([m["map"].replace("de_", ""), f'{won}-{m["rounds"] - won}',
                          num(mr["K/D"]), num(mr["ADR"], 1), num(mr["moving 1st shot%"], 1),
                          num(mr["util thrown/r"]), mr["open W/L"]])

    body = f"""<header class="masthead">
<p class="eyebrow">{esc(len(played))} matches &middot; {int(r["rounds"])} rounds &middot; <span class="role">{esc(role)}</span></p>
<h1>{esc(name)}</h1>
<p class="note">Measured against the job this player actually does. Role signals: first contact {num(sig["first_contact/r"])}/round, utility {num(sig["util/r"])}/round, AWP share {num(100 * sig["AWP kill share"], 0)}%, survival {num(sig["survival%"], 0)}%.</p>
</header>
<section>
<h2>The job</h2>
<div class="cards">{kpi_cards}</div>
</section>
<section>
<h2>By side</h2>
{table(["Side", "Rounds", "ADR", "1st duels", "Util/r"], sides)}
</section>
<section>
<h2>By half</h2>
{table(["Scope", "Rounds", "ADR", "K/D", "Moving 1st%", "Util/r", "1st duels"], halves)}
</section>
<section>
<h2>With the others</h2>
{table(["Teammate", "Trades for them", "They trade for", "Flashes into their kills", "Their flashes into yours", "Distance (m)"], pairs)}
</section>
<section>
<h2>Match by match</h2>
{table(["Map", "Result", "K/D", "ADR", "Moving 1st%", "Util/r", "1st duels"], per_match)}
</section>"""
    return page(name, body, generated,
                nav='<a href="team.html">&larr; Team report</a> &middot; <a href="index.html">Personal report</a>')


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
    pairs = [(l, v) for l, v in curated_averages(avg) if l in faceit_stats.HEADLINE]
    cards = "".join(
        f'<div class="card"><span class="label">{esc(label)}</span>'
        f'<span class="value">{num(value, 1)}</span></div>'
        for label, value in pairs)
    win = avg.get("_win_rate")
    head_cards = (f'<div class="card"><span class="label">maps</span>'
                  f'<span class="value">{int(avg.get("_maps", 0))}</span></div>'
                  + (f'<div class="card"><span class="label">win rate</span>'
                     f'<span class="value">{num(win, 0)}%</span></div>' if win is not None else ""))

    labels = faceit_stats.HEADLINE
    rows = []
    for row in sorted(stats, key=lambda r: r.get("finished_at") or 0, reverse=True):
        got = dict(faceit_stats.curated(row.get("stats", {})))
        when = row.get("finished_at")
        date = (dt.datetime.fromtimestamp(when, dt.timezone.utc).strftime("%Y-%m-%d")
                if isinstance(when, (int, float)) else "-")
        rows.append([date, (row.get("map") or "-").replace("de_", ""),
                     row.get("score") or "-", "W" if row.get("won") else "L"]
                    + [num(faceit_stats.as_number(got.get(l)), 1) if got.get(l) is not None else "-"
                       for l in labels])

    nav = '<a href="team.html">Team report</a>' if has_demos else ""
    body = f"""<header class="masthead">
<p class="eyebrow">from the FACEIT API &middot; no demo needed</p>
<h1>Recent form</h1>
<p class="note">Straight from FACEIT for every match in the window, refreshed on every run. These are the scoreboard and FACEIT's own advanced stats - the demo-only findings (whether you were moving when you fired, who trades for whom, how the halves differ) live in the team report.</p>
</header>
<section>
<h2>Averages across the window</h2>
<div class="cards">{head_cards}{cards}</div>
</section>
<section>
<h2>Match by match</h2>
{table(["Date", "Map", "Score", "W/L"] + labels, rows)}
<p class="note">Blank cells are stats FACEIT did not return for that match. The key names are undocumented and have changed before, so anything unrecognised is kept in the stored data even when it is not shown here.</p>
</section>"""
    return page("Recent form", body, generated, nav=nav)


def render(matches, out_dir, stats=()):
    out = pathlib.Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    generated = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    playable = [m for m in matches if ME in m.get("players", {})]
    stats = list(stats)

    if not playable:
        # No demo parsed yet: the API stats still make a useful front page.
        if stats:
            (out / "index.html").write_text(build_faceit(stats, generated, has_demos=False),
                                            encoding="utf-8")
        else:
            (out / "index.html").write_text(
                page("Where the rounds go", '<header class="masthead"><h1>No matches yet</h1>'
                     '<p class="note">No demo has been parsed and the FACEIT stats sync has '
                     'returned nothing yet.</p></header>', generated), encoding="utf-8")
        return [out / "index.html"]

    names = analyze.roster(playable)
    pooled = analyze.pooled_players(playable, names)
    roles = analyze.assign_roles({n: analyze.role_signals(pooled[n]) for n in names})

    pages = {"index.html": build_index(playable, generated),
             "team.html": build_team(playable, generated)}
    if stats:
        pages["faceit.html"] = build_faceit(stats, generated, has_demos=True)
    for n in names:
        pages[slug(n)] = build_player(playable, n, names, roles, generated)

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
