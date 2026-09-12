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
    return page("Where the rounds go", body, generated, nav='<a href="team.html">Team report &rarr;</a>')


def build_team(matches, generated):
    names = analyze.roster(matches)
    pooled = analyze.pooled_players(matches, names)
    sig = {n: analyze.role_signals(pooled[n]) for n in names}
    roles = analyze.assign_roles(sig)

    indiv = []
    for n in names:
        r = analyze.rates(pooled[n])
        indiv.append([n, f'<span class="role">{esc(roles[n])}</span>', int(r["rounds"]),
                      num(r["K/D"]), num(r["ADR"], 1), num(r["HS%"], 1), num(r["accuracy%"], 1),
                      num(r["moving 1st shot%"], 1), num(r["util thrown/r"]), r["open W/L"]])

    halves = []
    for n in names:
        h1 = analyze.scope_rates(pooled[n], "h1_")
        h2 = analyze.scope_rates(pooled[n], "h2_")
        delta = h2["ADR"] - h1["ADR"] if h1["rounds"] and h2["rounds"] else float("nan")
        halves.append([n, h1["rounds"], num(h1["ADR"], 1), num(h2["ADR"], 1), num(delta, 1),
                       num(h1["moving 1st shot%"], 1), num(h2["moving 1st shot%"], 1),
                       num(h1["util thrown/r"]), num(h2["util thrown/r"])])

    def matrix_table(key, mean, digits):
        m = analyze.pair_matrix(matches, key, names, mean=mean)
        rows = [[a] + [num(m[a][b], digits, dash="&mdash;") for b in names] for a in names]
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


def render(matches, out_dir):
    out = pathlib.Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    generated = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    playable = [m for m in matches if ME in m.get("players", {})]
    if not playable:
        (out / "index.html").write_text(
            page("Where the rounds go", '<header class="masthead"><h1>No matches yet</h1>'
                 '<p class="note">The analyzer has not parsed a demo with this player in it.</p></header>',
                 generated), encoding="utf-8")
        return [out / "index.html"]
    written = []
    for name, content in (("index.html", build_index(playable, generated)),
                          ("team.html", build_team(playable, generated))):
        (out / name).write_text(content, encoding="utf-8")
        written.append(out / name)
    return written


if __name__ == "__main__":
    import json
    import sys
    data = json.loads(pathlib.Path("results.json").read_text())
    for f in render(data, sys.argv[1] if len(sys.argv) > 1 else "report"):
        print("wrote", f)
