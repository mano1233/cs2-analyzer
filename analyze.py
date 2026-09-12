"""Per-player aim, utility, role and pairing stats across all unpacked demos.

Writes results.json (raw counts per match/player, plus pair matrices) and prints
mirithefish per match and the pooled you-vs-stack-vs-lobby table. team.py renders the
individual / role / pairing report from the same results.json.

Players are keyed by name: steamid columns go float64 when NaN-padded and lose precision.
"""
import json
import pathlib
from collections import defaultdict

import numpy as np
import pandas as pd
from demoparser2 import DemoParser

ROOT = pathlib.Path(__file__).parent
ME = "mirithefish"
TR = 64                       # ticks per second
TRADE_WINDOW = 5 * TR
EFFECTIVE_BLIND = 1.0         # seconds; shorter blinds rarely decide a duel
MOVING = 100.0                # u/s; above accurate counter-strafe speed for every gun
SAMPLE_EVERY = 4 * TR         # proximity sampling interval
UNIT_M = 0.01905              # Source unit -> metres (1 unit = 0.75 inch)
NON_GUNS = ("knife", "bayonet", "grenade", "flashbang", "smoke", "molotov", "incgrenade",
            "decoy", "c4", "taser", "hegrenade", "inferno", "world")
UTIL_ITEMS = {"Flashbang", "Smoke Grenade", "High Explosive Grenade", "Molotov", "Incendiary Grenade"}
UTIL_DMG_WEAPONS = {"hegrenade", "inferno", "molotov", "incgrenade"}
DET_EVENTS = {"flashbang_detonate": "flashes", "smokegrenade_detonate": "smokes",
              "hegrenade_detonate": "hes", "inferno_startburn": "mollies"}
AWPS = {"awp"}
SNIPERS = {"awp", "ssg08", "scar20", "g3sg1"}
PISTOLS = {"glock", "usp_silencer", "hkp2000", "p250", "fiveseven", "tec9", "cz75a",
           "deagle", "revolver", "elite", "p228"}
T_SIDE, CT_SIDE = 2, 3

# Economy buckets by team equipment value at freeze end. Conventions, not truths -
# named here so a page can say which thresholds it used.
ECO_MAX, FORCE_MAX = 5000, 20000
BUCKETS = ("eco", "force", "full")


def norm(w):
    w = str(w).lower()
    return w[7:] if w.startswith("weapon_") else w


def is_gun(w):
    w = norm(w)
    return not any(x in w for x in NON_GUNS)


def n_util(inv):
    return sum(1 for i in (inv if isinstance(inv, (list, np.ndarray)) else []) if i in UTIL_ITEMS)


def analyze(dem):
    p = DemoParser(str(dem))
    evs = set(p.list_game_events())
    mapname = p.parse_header().get("map_name")

    start = 0
    for name in ("round_announce_match_start", "begin_new_match"):
        if name in evs:
            df = p.parse_event(name)
            if len(df):
                start = int(df.tick.max())
                break

    freeze = np.sort(p.parse_event("round_freeze_end").tick.to_numpy())
    freeze = freeze[freeze >= start]
    rend = p.parse_event("round_end")
    rend = rend[(rend.tick >= start) & rend.winner.notna()].sort_values("tick")
    n_rounds = min(len(freeze), len(rend))
    freeze = freeze[:n_rounds]
    end_ticks = rend.tick.to_numpy()[:n_rounds]
    winners = [CT_SIDE if w == "CT" else T_SIDE for w in rend.winner.tolist()[:n_rounds]]

    def get(name, **kw):
        if name not in evs:
            return pd.DataFrame(columns=["tick"])
        df = p.parse_event(name, **kw)
        df = df[df.tick >= start].copy()
        df["r"] = np.searchsorted(freeze, df.tick.to_numpy(), side="right") - 1
        df = df[(df.r >= 0) & (df.r < n_rounds)]
        df["t"] = (df.tick - freeze[df.r.to_numpy()]) / TR
        return df

    D = get("player_death", player=["team_num", "inventory"])
    H = get("player_hurt", player=["team_num"])
    F = get("weapon_fire", player=["velocity_X", "velocity_Y"])
    B = get("player_blind", player=["team_num"])
    dets = {k: get(k) for k in DET_EVENTS}

    # team + utility owned at each freeze end
    T = p.parse_ticks(["team_num", "inventory"], ticks=[int(x) for x in freeze])
    T["r"] = np.searchsorted(freeze, T.tick.to_numpy())
    team_of = {(r, n): int(t) for r, n, t in zip(T.r, T.name, T.team_num)}
    names = sorted(set(T.name))

    for c in ("attacker_team_num", "user_team_num", "assister_team_num"):
        D[c] = D[c].astype(float)
    for c in ("attacker_team_num", "user_team_num"):
        H[c] = H[c].astype(float)
        B[c] = B[c].astype(float)
    enemy_kill = D.attacker_team_num != D.user_team_num
    enemy_hurt = H.attacker_team_num != H.user_team_num
    first_death = D.sort_values("tick").groupby("r").first()
    has_weapon = "weapon" in D.columns

    # ---- per-round table -----------------------------------------------------
    # One row per round, from which the opening-kill, post-plant and economy
    # metrics are all aggregations. Round-level data also lets the rollup answer
    # questions the per-player counters cannot.
    plants = get("bomb_planted", player=["team_num", "X", "Y", "Z"])
    defuses = get("bomb_defused", player=["team_num"])
    equip = p.parse_ticks(["current_equip_value", "team_num"], ticks=[int(x) for x in freeze])
    equip["r"] = np.searchsorted(freeze, equip.tick.to_numpy())
    team_equip = {(int(r), int(team)): float(v) for (r, team), v
                  in equip.groupby(["r", "team_num"]).current_equip_value.sum().items()}

    def bucket(value):
        if value < ECO_MAX:
            return "eco"
        return "force" if value < FORCE_MAX else "full"

    rounds = []
    for r in range(n_rounds):
        fd = first_death.loc[r] if r in first_death.index else None
        plant = plants[plants.r == r] if len(plants) else plants
        defuse = defuses[defuses.r == r] if len(defuses) else defuses
        row = {
            "round": r + 1,
            "half": 1 if r < 12 else (2 if r < 24 else 3),
            "winner": winners[r],
            "opening_kill_team": (int(fd.attacker_team_num)
                                  if fd is not None and fd.attacker_team_num == fd.attacker_team_num
                                  else None),
            "opening_killer": None if fd is None else fd.attacker_name,
            "opening_victim": None if fd is None else fd.user_name,
            "opening_time": None if fd is None else float(fd.t),
            "planted": bool(len(plant)),
            "plant_site": int(plant.iloc[0].site) if len(plant) else None,
            "plant_x": float(plant.iloc[0].user_X) if len(plant) else None,
            "plant_y": float(plant.iloc[0].user_Y) if len(plant) else None,
            "plant_z": float(plant.iloc[0].user_Z) if len(plant) else None,
            "plant_time": float(plant.iloc[0].t) if len(plant) else None,
            "defused": bool(len(defuse)),
        }
        for side, tag in ((T_SIDE, "t"), (CT_SIDE, "ct")):
            value = team_equip.get((r, side), 0.0)
            row[f"{tag}_equip"] = value
            row[f"{tag}_bucket"] = bucket(value)
        rounds.append(row)

    # ---- pair matrices -------------------------------------------------------
    # traded_for[A][B]: A killed the player who had just killed teammate B
    # flash_conv[A][B]: A blinded an enemy that B then killed inside the blind
    # prox_sum/prox_n[A][B]: sampled distance between living teammates A and B
    traded_for = defaultdict(lambda: defaultdict(int))
    flash_conv = defaultdict(lambda: defaultdict(int))
    prox_sum = defaultdict(lambda: defaultdict(float))
    prox_n = defaultdict(lambda: defaultdict(int))

    for _, d in D.iterrows():
        if d.attacker_name is None or d.attacker_team_num == d.user_team_num:
            continue
        revenge = D[(D.user_name == d.attacker_name) & (D.tick > d.tick)
                    & (D.tick <= d.tick + TRADE_WINDOW) & (D.attacker_team_num == d.user_team_num)]
        for _, rv in revenge.iterrows():
            if rv.attacker_name and rv.attacker_name != d.user_name:
                traded_for[rv.attacker_name][d.user_name] += 1

    eff_all = B[(B.user_team_num != B.attacker_team_num) & (B.blind_duration >= EFFECTIVE_BLIND)]
    for _, b in eff_all.iterrows():
        conv = D[(D.user_name == b.user_name) & (D.tick >= b.tick)
                 & (D.tick <= b.tick + b.blind_duration * TR)
                 & (D.attacker_team_num == b.attacker_team_num)]
        for _, k in conv.iterrows():
            if k.attacker_name:
                flash_conv[b.attacker_name][k.attacker_name] += 1

    samples = []
    for r in range(n_rounds):
        t = freeze[r] + SAMPLE_EVERY
        while t < end_ticks[r]:
            samples.append(int(t))
            t += SAMPLE_EVERY
    if samples:
        S = p.parse_ticks(["X", "Y", "team_num", "health"], ticks=samples)
        S = S[S.health > 0]
        for tick, grp in S.groupby("tick"):
            for team, side in grp.groupby("team_num"):
                rows = list(zip(side.name, side.X.astype(float), side.Y.astype(float)))
                for i in range(len(rows)):
                    for j in range(i + 1, len(rows)):
                        a, ax, ay = rows[i]
                        b, bx, by = rows[j]
                        dist = float(np.hypot(ax - bx, ay - by))
                        prox_sum[a][b] += dist
                        prox_n[a][b] += 1
                        prox_sum[b][a] += dist
                        prox_n[b][a] += 1

    # ---- per-player counters -------------------------------------------------
    # Everything countable is computed through one scoped bundle, so overall, per
    # side and per half come from the same code path instead of three copies.
    # MR12: rounds 0-11 are the first half, 12-23 the second, 24+ overtime.
    scopes = {"h1": set(range(0, 12)), "h2": set(range(12, 24)),
              "ot": set(range(24, max(n_rounds, 24)))}

    out = {}
    for name in names:
        s = defaultdict(float)
        my_rounds = [r for r in range(n_rounds) if (r, name) in team_of]
        s["rounds_won"] = sum(1 for r in my_rounds if team_of[(r, name)] == winners[r])
        s["t_rounds"] = sum(1 for r in my_rounds if team_of[(r, name)] == T_SIDE)
        s["ct_rounds"] = sum(1 for r in my_rounds if team_of[(r, name)] == CT_SIDE)

        kills = D[(D.attacker_name == name) & enemy_kill]
        deaths = D[D.user_name == name]
        assists = D[(D.assister_name == name) & (D.assister_team_num != D.user_team_num)]
        mine_h = H[(H.attacker_name == name) & enemy_hurt]
        shots = F[(F.user_name == name) & F.weapon.map(is_gun)].sort_values("tick")
        first = shots[shots.tick.diff().fillna(1e9) > TR // 2]
        moving = first[np.hypot(first.user_velocity_X.astype(float),
                                first.user_velocity_Y.astype(float)) > MOVING]
        opened = first_death[first_death.attacker_name == name].index
        got_opened = first_death[first_death.user_name == name].index

        s["assists"] = len(assists)
        s["flash_assists"] = int(assists.assistedflash.fillna(False).sum())
        s["noscope_kills"] = int(kills.noscope.fillna(False).sum()) if "noscope" in kills else 0
        s["thrusmoke_kills"] = int(kills.thrusmoke.fillna(False).sum()) if "thrusmoke" in kills else 0
        s["death_time_sum"] = float(deaths.t.sum())
        s["kill_dist_sum"] = float(kills.distance.sum()) if "distance" in kills else 0.0
        s["util_damage"] = float(mine_h[mine_h.weapon.map(norm).isin(UTIL_DMG_WEAPONS)].dmg_health.sum())

        if has_weapon:
            w = kills.weapon.map(norm)
            s["awp_kills"] = int(w.isin(AWPS).sum())
            s["sniper_kills"] = int(w.isin(SNIPERS).sum())
            s["pistol_kills"] = int(w.isin(PISTOLS).sum())

        # Per-event flags once, summed per scope afterwards.
        traded_rounds, held_util, trade_kill_rounds = [], [], []
        for _, d in deaths.iterrows():
            revenge = D[(D.user_name == d.attacker_name) & (D.tick > d.tick)
                        & (D.tick <= d.tick + TRADE_WINDOW) & (D.attacker_team_num == d.user_team_num)]
            traded_rounds.append((d.r, len(revenge) > 0))
            held_util.append((d.r, n_util(d.user_inventory)))
            s["died_with_util_rounds"] += int(n_util(d.user_inventory) > 0)
            s["util_lost_on_death"] += n_util(d.user_inventory)
        for _, k in kills.iterrows():
            avenged = D[(D.attacker_name == k.user_name) & (D.tick < k.tick)
                        & (D.tick >= k.tick - TRADE_WINDOW) & (D.user_team_num == k.attacker_team_num)]
            trade_kill_rounds.append((k.r, len(avenged) > 0))

        util_events = []
        for ev, key in DET_EVENTS.items():
            mine = dets[ev][dets[ev].user_name == name]
            s[key] = len(mine)
            for _, u in mine.iterrows():
                util_events.append((u.r, u.tick, u.t, team_of.get((u.r, name)), ev))
        for r, tick, t, team, ev in util_events:
            fd = first_death.tick.get(r)
            s["util_before_contact"] += int(fd is None or tick < fd)
            tag = "t" if team == T_SIDE else "ct"
            s[f"util_time_sum_{tag}"] += t
            s[f"util_n_{tag}"] += 1
        s["util_owned"] = int(T[T.name == name].inventory.map(n_util).sum())

        # Time to first contact: first moment in the round this player deals or
        # takes damage. Pairs with the utility-timing numbers - first contact at
        # 20s while your first utility lands at 45s means you threw it too late.
        contact = H[(H.attacker_name == name) | (H.user_name == name)]
        for r_, first_t in contact.groupby("r").t.min().items():
            side = team_of.get((int(r_), name))
            tag = "t" if side == T_SIDE else "ct"
            s[f"first_contact_sum_{tag}"] += float(first_t)
            s[f"first_contact_n_{tag}"] += 1

        # This player's own opening duels, and whether the round was then won.
        for row in rounds:
            if row["opening_killer"] == name:
                s["own_opening_kills"] += 1
                s["own_opening_kill_wins"] += int(team_of.get((row["round"] - 1, name)) == row["winner"])
            if row["opening_victim"] == name:
                s["own_opening_deaths"] += 1
                s["own_opening_death_wins"] += int(team_of.get((row["round"] - 1, name)) == row["winner"])

        eff_flashes = B[(B.attacker_name == name) & (B.user_team_num != B.attacker_team_num)
                        & (B.blind_duration >= EFFECTIVE_BLIND)]

        def bundle(prefix, rounds):
            rs = set(rounds) & set(my_rounds)
            if prefix and not rs:
                return
            s[prefix + "rounds"] = len(rs)
            s[prefix + "kills"] = int(kills.r.isin(rs).sum())
            s[prefix + "deaths"] = int(deaths.r.isin(rs).sum())
            s[prefix + "hs_kills"] = int(kills[kills.r.isin(rs)].headshot.fillna(False).sum())
            s[prefix + "open_won"] = int(opened.isin(rs).sum())
            s[prefix + "open_lost"] = int(got_opened.isin(rs).sum())
            s[prefix + "damage"] = float(mine_h[mine_h.r.isin(rs)].dmg_health.clip(upper=100).sum())
            s[prefix + "hits"] = int(mine_h[mine_h.r.isin(rs)].weapon.map(is_gun).sum())
            s[prefix + "shots"] = int(shots.r.isin(rs).sum())
            s[prefix + "first_shots"] = int(first.r.isin(rs).sum())
            s[prefix + "first_shots_moving"] = int(moving.r.isin(rs).sum())
            s[prefix + "util_thrown"] = sum(1 for r, *_ in util_events if r in rs)
            s[prefix + "flashes"] = sum(1 for r, _, _, _, ev in util_events
                                        if r in rs and ev == "flashbang_detonate")
            s[prefix + "flashes_hit"] = int(eff_flashes[eff_flashes.r.isin(rs)].tick.nunique())
            s[prefix + "deaths_traded"] = sum(1 for r, ok in traded_rounds if r in rs and ok)
            s[prefix + "trade_kills"] = sum(1 for r, ok in trade_kill_rounds if r in rs and ok)

        bundle("", range(n_rounds))
        bundle("t_", [r for r in my_rounds if team_of[(r, name)] == T_SIDE])
        bundle("ct_", [r for r in my_rounds if team_of[(r, name)] == CT_SIDE])
        for tag, rs in scopes.items():
            bundle(tag + "_", rs)

        myb = B[B.attacker_name == name]
        eff = myb[(myb.user_team_num != myb.attacker_team_num) & (myb.blind_duration >= EFFECTIVE_BLIND)]
        s["enemy_blinds"] = len(eff)
        s["enemy_blind_secs"] = float(eff.blind_duration.sum())
        s["flashes_hit"] = int(eff.tick.nunique())
        s["team_blinds"] = int(((myb.user_team_num == myb.attacker_team_num) & (myb.user_name != name)
                                & (myb.blind_duration >= EFFECTIVE_BLIND)).sum())
        s["self_blinds"] = int(((myb.user_name == name) & (myb.blind_duration >= EFFECTIVE_BLIND)).sum())
        s["flash_kills"] = sum(flash_conv[name].values())
        out[name] = dict(s)

    # roster by majority of rounds, not round 0: a player who connects late or
    # misses the first freeze end used to drop out of the comparison entirely.
    def share_with_me(n):
        both = [r for r in range(n_rounds) if (r, n) in team_of and (r, ME) in team_of]
        if not both:
            return 0.0
        return sum(1 for r in both if team_of[(r, n)] == team_of[(r, ME)]) / len(both)

    stack = [n for n in names if share_with_me(n) > 0.6]

    pack = lambda m: {a: dict(b) for a, b in m.items()}
    return {"demo": dem.name, "map": mapname, "rounds": n_rounds, "stack": stack,
            "round_table": rounds,
            "players": out, "traded_for": pack(traded_for), "flash_conv": pack(flash_conv),
            "prox_sum": pack(prox_sum), "prox_n": pack(prox_n)}


def rates(s):
    s = defaultdict(float, s)  # counters never incremented are absent from saved dicts
    r = max(s["rounds"], 1)
    div = lambda a, b: a / b if b else float("nan")
    return {
        "rounds": int(s["rounds"]),
        "K/D": div(s["kills"], s["deaths"]),
        "ADR": s["damage"] / r,
        "HS%": 100 * div(s["hs_kills"], s["kills"]),
        "open W/L": f'{int(s["open_won"])}/{int(s["open_lost"])}',
        "traded death%": 100 * div(s["deaths_traded"], s["deaths"]),
        "trade kills/r": s["trade_kills"] / r,
        "accuracy%": 100 * div(s["hits"], s["shots"]),
        "moving 1st shot%": 100 * div(s["first_shots_moving"], s["first_shots"]),
        "util thrown/r": s["util_thrown"] / r,
        "util used% of owned": 100 * div(s["util_thrown"], s["util_owned"]),
        "flashes/r": s["flashes"] / r,
        "smokes/r": s["smokes"] / r,
        "mollies/r": s["mollies"] / r,
        "HEs/r": s["hes"] / r,
        "flash hit% (>=1 enemy)": 100 * div(s["flashes_hit"], s["flashes"]),
        "enemies blinded/flash": div(s["enemy_blinds"], s["flashes"]),
        "team blinds/flash": div(s["team_blinds"], s["flashes"]),
        "flash->kill": int(s["flash_kills"]),
        "flash assists": int(s["flash_assists"]),
        "util dmg/r": s["util_damage"] / r,
        "util before 1st kill%": 100 * div(s["util_before_contact"], s["util_thrown"]),
        "avg util time T (s)": div(s["util_time_sum_t"], s["util_n_t"]),
        "avg util time CT (s)": div(s["util_time_sum_ct"], s["util_n_ct"]),
        "died holding util%": 100 * div(s["died_with_util_rounds"], s["deaths"]),
    }


def scope_rates(s, prefix=""):
    """Rates for one scope: prefix "" overall, "t_"/"ct_" sides, "h1_"/"h2_"/"ot_" halves."""
    s = defaultdict(float, s)
    r = max(s[prefix + "rounds"], 1)
    div = lambda a, b: a / b if b else float("nan")
    return {
        "rounds": int(s[prefix + "rounds"]),
        "K/D": div(s[prefix + "kills"], s[prefix + "deaths"]),
        "ADR": s[prefix + "damage"] / r,
        "HS%": 100 * div(s[prefix + "hs_kills"], s[prefix + "kills"]),
        "open W/L": f'{int(s[prefix + "open_won"])}/{int(s[prefix + "open_lost"])}',
        "accuracy%": 100 * div(s[prefix + "hits"], s[prefix + "shots"]),
        "moving 1st shot%": 100 * div(s[prefix + "first_shots_moving"], s[prefix + "first_shots"]),
        "util thrown/r": s[prefix + "util_thrown"] / r,
        "flash hit%": 100 * div(s[prefix + "flashes_hit"], s[prefix + "flashes"]),
        "traded death%": 100 * div(s[prefix + "deaths_traded"], s[prefix + "deaths"]),
        "trade kills/r": s[prefix + "trade_kills"] / r,
    }


def pool(dicts):
    tot = defaultdict(float)
    for d in dicts:
        for k, v in d.items():
            tot[k] += v
    return tot



# ---- shared team helpers (used by team.py and report.py) --------------------

def roster(matches, me=ME, min_share=0.5):
    """Stack-mates present in at least half the matches, me first. Stand-ins drop out."""
    seen = defaultdict(int)
    for m in matches:
        for n in m.get("stack", []):
            seen[n] += 1
    mates = [n for n, c in seen.items() if c >= len(matches) * min_share and n != me]
    return [me] + sorted(mates, key=lambda n: (-seen[n], n))


def pooled_players(matches, names):
    return {n: pool(m["players"][n] for m in matches if n in m["players"]) for n in names}


def role_signals(s):
    """What a player actually does per round - the inputs to the role rule."""
    s = defaultdict(float, s)
    r = max(s["rounds"], 1)
    div = lambda a, b: a / b if b else float("nan")
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


def assign_roles(sig):
    """Roles from measured behaviour. The rule is deliberately simple and stated in
    the output, so a player can disagree with it rather than be labelled silently."""
    names = list(sig)
    med = lambda key: sorted(sig[n][key] for n in names)[len(names) // 2]
    top_entry = max(sig[n]["T open/r"] for n in names)
    roles = {}
    for n in names:
        g = sig[n]
        if g["AWP kill share"] >= 0.20:
            roles[n] = "AWP"
        elif g["T open/r"] == top_entry and g["T open/r"] >= 0.15:
            roles[n] = "Entry"
        elif g["util/r"] >= med("util/r") and g["smokes+flashes/r"] >= med("smokes+flashes/r"):
            roles[n] = "Support"
        elif g["avg death time (s)"] >= med("avg death time (s)") and g["first_contact/r"] <= med("first_contact/r"):
            roles[n] = "Lurk / late"
        else:
            roles[n] = "Rifler"
    return roles


ROLE_RULE = ("AWP if at least 20% of kills come with the AWP; else Entry for the highest "
             "T-side first-contact rate (and at least 0.15/round); else Support if both "
             "utility per round and smokes+flashes per round are at or above the team "
             "median; else Lurk/late for dying later than the median with below-median "
             "first contact; else Rifler.")

ROLE_KPIS = {
    "Entry": ["open W/L", "traded death%", "moving 1st shot%", "accuracy%", "ADR"],
    "Support": ["util thrown/r", "flash hit% (>=1 enemy)", "flash->kill", "util before 1st kill%",
                "died holding util%", "avg util time T (s)"],
    "AWP": ["open W/L", "accuracy%", "ADR", "K/D"],
    "Lurk / late": ["trade kills/r", "traded death%", "K/D", "ADR"],
    "Rifler": ["ADR", "HS%", "accuracy%", "trade kills/r", "K/D"],
}


def round_metrics(matches, side=None):
    """Team metrics aggregated over the per-round tables of several matches.

    `side` limits to T (2) or CT (3) from the perspective of that side; None returns
    both sides' numbers keyed by side. The opening kill is what creates the 5v4, so
    conversion and save rate are one computation with two outputs.
    """
    rows = [r for m in matches for r in m.get("round_table", [])]
    out = {"rounds": len(rows)}
    if not rows:
        return out

    for tag, team in (("t", T_SIDE), ("ct", CT_SIDE)):
        if side is not None and team != side:
            continue
        got_opening = [r for r in rows if r["opening_kill_team"] == team]
        lost_opening = [r for r in rows if r["opening_kill_team"] not in (team, None)]
        won = lambda rs: 100 * sum(1 for r in rs if r["winner"] == team) / len(rs) if rs else float("nan")
        out[f"{tag}_rounds"] = sum(1 for r in rows if True)
        out[f"{tag}_opening_kills"] = len(got_opening)
        out[f"{tag}_opening_conversion%"] = won(got_opening)
        out[f"{tag}_opening_deaths"] = len(lost_opening)
        out[f"{tag}_save_after_opening_death%"] = won(lost_opening)

        for b in BUCKETS:
            in_bucket = [r for r in rows if r[f"{tag}_bucket"] == b]
            out[f"{tag}_{b}_rounds"] = len(in_bucket)
            out[f"{tag}_{b}_win%"] = won(in_bucket)

    planted = [r for r in rows if r["planted"]]
    out["plants"] = len(planted)
    out["plant_rate%"] = 100 * len(planted) / len(rows)
    out["post_plant_t_win%"] = (100 * sum(1 for r in planted if r["winner"] == T_SIDE) / len(planted)
                                if planted else float("nan"))
    out["defuse_rate%"] = (100 * sum(1 for r in planted if r["defused"]) / len(planted)
                           if planted else float("nan"))
    out["sites"] = plant_sites(matches)
    return out


def plant_sites(matches, max_spread=600.0):
    """Bomb sites per map, clustered from plant coordinates.

    The demo's `site` field is an entity index that changes between demos of the same
    map - six demos over four maps produced twelve distinct ids - so it cannot identify
    a site. Plant coordinates can: the two sites on a map are hundreds of units apart,
    far beyond where individual plants scatter.

    Nuke is the exception that shapes this: its sites are stacked vertically, only a
    few hundred units apart in X/Y but far apart in Z, so height is included in the
    distance.

    Returns {map: [{x, y, z, plants, t_win%, defused%}]}, ordered by plant count. Naming
    them A and B is a one-time human judgement per map, not something to guess here.
    """
    by_map = defaultdict(list)
    for m in matches:
        for r in m.get("round_table", []):
            if r["planted"] and r["plant_x"] is not None:
                by_map[m["map"]].append(r)

    out = {}
    for mapname, plants in by_map.items():
        xs = sorted(plants, key=lambda r: r["plant_x"])
        far = max(plants, key=lambda r: (r["plant_x"] - xs[0]["plant_x"]) ** 2
                                        + (r["plant_y"] - xs[0]["plant_y"]) ** 2
                                        + (r.get("plant_z") or 0 - (xs[0].get("plant_z") or 0)) ** 2)
        seeds = [(xs[0]["plant_x"], xs[0]["plant_y"], xs[0].get("plant_z") or 0.0),
                 (far["plant_x"], far["plant_y"], far.get("plant_z") or 0.0)]
        # Two far-apart seeds and one assignment pass: sites are separated by far more
        # than max_spread, so iterating to convergence buys nothing.
        groups = [[], []]
        for r in plants:
            d = [((r["plant_x"] - sx) ** 2 + (r["plant_y"] - sy) ** 2
                  + ((r.get("plant_z") or 0.0) - sz) ** 2) ** 0.5 for sx, sy, sz in seeds]
            groups[0 if d[0] <= d[1] else 1].append(r)
        clusters = []
        for g in groups:
            if not g:
                continue
            clusters.append({
                "x": round(sum(r["plant_x"] for r in g) / len(g)),
                "y": round(sum(r["plant_y"] for r in g) / len(g)),
                "z": round(sum(r.get("plant_z") or 0.0 for r in g) / len(g)),
                "plants": len(g),
                "t_win%": 100 * sum(1 for r in g if r["winner"] == T_SIDE) / len(g),
                "defused%": 100 * sum(1 for r in g if r["defused"]) / len(g),
            })
        out[mapname] = sorted(clusters, key=lambda c: -c["plants"])
    return out


def pair_matrix(matches, key, names, mean=False):
    """Pair values over the roster: rows act on columns. mean=True averages prox_sum
    over prox_n (metres); otherwise counts are summed."""
    tot = defaultdict(lambda: defaultdict(float))
    cnt = defaultdict(lambda: defaultdict(float))
    for m in matches:
        for a, row in m.get("prox_sum" if mean else key, {}).items():
            for b, v in row.items():
                tot[a][b] += v
        if mean:
            for a, row in m.get("prox_n", {}).items():
                for b, v in row.items():
                    cnt[a][b] += v
    out = {}
    for a in names:
        out[a] = {}
        for b in names:
            if a == b:
                out[a][b] = None
            elif mean:
                out[a][b] = (tot[a][b] / cnt[a][b] * UNIT_M) if cnt[a][b] else None
            else:
                out[a][b] = tot[a][b]
    return out

if __name__ == "__main__":
    import sys
    if "--cached" in sys.argv:
        matches = json.loads((ROOT / "results.json").read_text())
    else:
        matches = [analyze(d) for d in sorted(ROOT.glob("*.dem"))]
        (ROOT / "results.json").write_text(json.dumps(matches, indent=1, default=float))

    pd.set_option("display.width", 250, "display.max_columns", 50, "display.float_format", "{:.2f}".format)
    per_match = {}
    for m in matches:
        me = m["players"][ME]
        row = rates(me)
        row["result"] = f'{int(me["rounds_won"])}-{m["rounds"] - int(me["rounds_won"])}'
        per_match[f'{m["map"]} {m["demo"][2:10]}'] = row
    print("=== mirithefish per match ===")
    print(pd.DataFrame(per_match).to_string())

    stack_names = set(matches[0]["stack"])
    for m in matches[1:]:
        stack_names &= set(m["stack"])
    cols = {}
    for n in [ME] + sorted(stack_names - {ME}):
        cols[n] = rates(pool(m["players"][n] for m in matches if n in m["players"]))
    lobby = [pl for m in matches for pl in m["players"].values()]
    cols["lobby avg (per player)"] = rates(pool(lobby))
    print("\n=== all matches pooled: you vs stack vs lobby ===")
    print(pd.DataFrame(cols).to_string())
