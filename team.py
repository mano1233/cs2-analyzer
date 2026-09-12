"""Team report from results.json: individual performance, roles, role-specific stats,
and how the five play off one another (trades, flash conversions, proximity).

Roles are assigned from measured signals, never assumed - the rule is printed with the
table so you can disagree with it. Run: .venv/Scripts/python team.py
"""
import json
import pathlib
from collections import defaultdict

import pandas as pd

import analyze
from analyze import ME, UNIT_M, pool, rates

ROOT = pathlib.Path(__file__).parent
MATCHES = json.loads((ROOT / "results.json").read_text())
pd.set_option("display.width", 250, "display.max_columns", 60, "display.float_format", "{:.2f}".format)


def div(a, b):
    return a / b if b else float("nan")


# ---- roster: stack-mates in at least half the matches (stand-ins excluded) ----
appearances = defaultdict(int)
for m in MATCHES:
    for n in m["stack"]:
        appearances[n] += 1
STACK = [n for n, c in sorted(appearances.items(), key=lambda kv: -kv[1]) if c >= len(MATCHES) / 2]
STACK = [ME] + [n for n in STACK if n != ME]
POOLED = {n: pool(m["players"][n] for m in MATCHES if n in m["players"]) for n in STACK}
LOBBY = pool(pl for m in MATCHES for pl in m["players"].values())


def signals(s):
    """Role signals: what a player actually does, per round."""
    r = max(s["rounds"], 1)
    return {
        "rounds": int(s["rounds"]),
        "first_contact/r": (s["open_won"] + s["open_lost"]) / r,
        "T open/r": div(s["t_open_won"] + s["t_open_lost"], max(s["t_rounds"], 1)),
        "CT open/r": div(s["ct_open_won"] + s["ct_open_lost"], max(s["ct_rounds"], 1)),
        "util/r": s["util_thrown"] / r,
        "smokes+flashes/r": (s["smokes"] + s["flashes"]) / r,
        "AWP kill share": div(s["awp_kills"], s["kills"]),
        "trade kills/r": s["trade_kills"] / r,
        "avg death time (s)": div(s["death_time_sum"], s["deaths"]),
        "survival%": 100 * (1 - div(s["deaths"], r)),
    }


SIG = {n: signals(POOLED[n]) for n in STACK}
med = lambda key: sorted(SIG[n][key] for n in STACK)[len(STACK) // 2]


def role_of(n):
    s, sg = POOLED[n], SIG[n]
    if sg["AWP kill share"] >= 0.20:
        return "AWP"
    top_entry = max(SIG[x]["T open/r"] for x in STACK)
    if sg["T open/r"] == top_entry and sg["T open/r"] >= 0.15:
        return "Entry"
    if sg["util/r"] >= med("util/r") and sg["smokes+flashes/r"] >= med("smokes+flashes/r"):
        return "Support"
    if sg["avg death time (s)"] >= med("avg death time (s)") and sg["first_contact/r"] <= med("first_contact/r"):
        return "Lurk / late"
    return "Rifler"


ROLE = {n: role_of(n) for n in STACK}

ROLE_KPIS = {
    "Entry": ["open W/L", "traded death%", "moving 1st shot%", "accuracy%", "ADR"],
    "Support": ["util thrown/r", "flash hit% (>=1 enemy)", "flash->kill", "util before 1st kill%",
                "died holding util%", "avg util time T (s)"],
    "AWP": ["open W/L", "accuracy%", "ADR", "K/D"],
    "Lurk / late": ["trade kills/r", "traded death%", "K/D", "ADR"],
    "Rifler": ["ADR", "HS%", "accuracy%", "trade kills/r", "K/D"],
}


def matrix(key, scale=1.0, mean=False):
    """Pair matrix over the stack: rows act on columns."""
    tot = defaultdict(lambda: defaultdict(float))
    cnt = defaultdict(lambda: defaultdict(float))
    for m in MATCHES:
        for a, row in m.get(key if not mean else "prox_sum", {}).items():
            for b, v in row.items():
                tot[a][b] += v
        if mean:
            for a, row in m.get("prox_n", {}).items():
                for b, v in row.items():
                    cnt[a][b] += v
    df = pd.DataFrame(index=STACK, columns=STACK, dtype=float)
    for a in STACK:
        for b in STACK:
            if a == b:
                df.loc[a, b] = float("nan")
            elif mean:
                df.loc[a, b] = (tot[a][b] / cnt[a][b] * scale) if cnt[a][b] else float("nan")
            else:
                df.loc[a, b] = tot[a][b] * scale
    return df


print("=== roster ===")
for n in STACK:
    print("  %-13s %d/%d matches, %d rounds, role: %s"
          % (n, appearances[n], len(MATCHES), POOLED[n]["rounds"], ROLE[n]))
print("\nRole rule: AWP if >=20%% of kills with the AWP; else Entry if the highest T-side")
print("first-contact rate in the team and >=0.15/round; else Support if both utility per")
print("round and smokes+flashes per round are at or above the team median; else Lurk/late")
print("if dying later than the median with below-median first contact; else Rifler.")

print("\n=== 1. individual performance (pooled, vs lobby average) ===")
cols = {n: rates(POOLED[n]) for n in STACK}
cols["lobby avg"] = rates(LOBBY)
print(pd.DataFrame(cols).to_string())

print("\n=== 2. role signals (what each player actually does) ===")
print(pd.DataFrame({n: SIG[n] for n in STACK}).to_string())

print("\n=== 3. role-specific stats (each player judged on their own job) ===")
for n in STACK:
    r = rates(POOLED[n])
    keys = ROLE_KPIS[ROLE[n]]
    line = "  ".join("%s %s" % (k, ("%.2f" % r[k]) if isinstance(r[k], float) else r[k]) for k in keys)
    print("%-13s [%-11s] %s" % (n, ROLE[n], line))

print("\n=== 4. per half (MR12: rounds 1-12, 13-24, then OT) ===")
HALF_METRICS = ["ADR", "K/D", "moving 1st shot%", "util thrown/r", "open W/L", "traded death%"]
for metric in HALF_METRICS:
    rows = {}
    for n in STACK:
        h1 = analyze.scope_rates(POOLED[n], "h1_")
        h2 = analyze.scope_rates(POOLED[n], "h2_")
        ot = analyze.scope_rates(POOLED[n], "ot_")
        row = {"1st half": h1[metric], "2nd half": h2[metric]}
        if ot["rounds"]:
            row["OT"] = ot[metric]
        if not isinstance(h1[metric], str):
            row["change"] = h2[metric] - h1[metric]
        rows[n] = row
    print("\n-- %s --" % metric)
    print(pd.DataFrame(rows).to_string())

print("\n-- rounds per scope (sanity check) --")
print(pd.DataFrame({n: {k: analyze.scope_rates(POOLED[n], p)["rounds"]
                        for k, p in (("1st half", "h1_"), ("2nd half", "h2_"), ("OT", "ot_"),
                                     ("T", "t_"), ("CT", "ct_"), ("all", ""))}
                    for n in STACK}).to_string())

print("\n=== 5. how you play off one another ===")
print("\n-- trades: row trades FOR column (row killed the enemy who had just killed column) --")
print(matrix("traded_for").to_string())
print("\n-- flash conversions: row's flash, column got the kill --")
print(matrix("flash_conv").to_string())
print("\n-- average distance between teammates while both alive, in metres --")
print(matrix("prox_sum", scale=UNIT_M, mean=True).to_string())

prox = matrix("prox_sum", scale=UNIT_M, mean=True)
trades = matrix("traded_for")
print("\n-- per player: closest partner, who covers them, who they cover --")
for n in STACK:
    near = prox.loc[n].idxmin() if prox.loc[n].notna().any() else "-"
    covered_by = trades[n].idxmax() if trades[n].notna().any() and trades[n].max() > 0 else "nobody"
    covers = trades.loc[n].idxmax() if trades.loc[n].notna().any() and trades.loc[n].max() > 0 else "nobody"
    print("%-13s plays nearest %-12s (%.0f m)   most often traded by %-12s   trades most for %s"
          % (n, near, prox.loc[n].min(), covered_by, covers))
