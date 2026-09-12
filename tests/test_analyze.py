"""Metric maths, roster and role assignment - the parts that decide what a number means.

No demo parsing here: parsing is demoparser2's job and needs a 200 MB fixture. These
cover the layer that turns raw counters into claims about a player.
"""
import math

import pytest

import analyze


def counters(**over):
    """A player's raw counters, with sane defaults so a test states only what it means."""
    base = {
        "rounds": 100, "kills": 50, "deaths": 60, "hs_kills": 20, "damage": 7000.0,
        "hits": 100, "shots": 500, "first_shots": 200, "first_shots_moving": 60,
        "open_won": 10, "open_lost": 15, "deaths_traded": 12, "trade_kills": 8,
        "util_thrown": 100, "util_owned": 200, "flashes": 20, "flashes_hit": 5,
        "smokes": 30, "hes": 25, "mollies": 25, "enemy_blinds": 6, "team_blinds": 2,
        "flash_kills": 3, "flash_assists": 1, "util_damage": 400.0, "util_before_contact": 50,
        "died_with_util_rounds": 30, "death_time_sum": 3000.0, "awp_kills": 0,
        "t_rounds": 50, "ct_rounds": 50, "t_open_won": 5, "t_open_lost": 8,
        "ct_open_won": 5, "ct_open_lost": 7,
    }
    base.update(over)
    return base


class TestWeaponHelpers:
    @pytest.mark.parametrize("raw,expected", [
        ("weapon_ak47", "ak47"), ("AK47", "ak47"), ("weapon_m4a1_silencer", "m4a1_silencer"),
    ])
    def test_norm_strips_prefix_and_case(self, raw, expected):
        assert analyze.norm(raw) == expected

    @pytest.mark.parametrize("weapon", ["weapon_ak47", "awp", "deagle", "mp9"])
    def test_guns_count_as_guns(self, weapon):
        assert analyze.is_gun(weapon)

    @pytest.mark.parametrize("weapon", ["knife", "hegrenade", "inferno", "molotov",
                                        "flashbang", "smokegrenade", "world", "c4", "taser"])
    def test_non_guns_excluded(self, weapon):
        """Accuracy is a gun statistic: grenade damage must not inflate it."""
        assert not analyze.is_gun(weapon)

    def test_n_util_counts_only_grenades(self):
        inv = ["AK-47", "Flashbang", "Smoke Grenade", "Kevlar", "Molotov"]
        assert analyze.n_util(inv) == 3

    def test_n_util_survives_missing_inventory(self):
        assert analyze.n_util(None) == 0
        assert analyze.n_util(float("nan")) == 0


class TestRates:
    def test_core_rates(self):
        r = analyze.rates(counters())
        assert r["K/D"] == pytest.approx(50 / 60)
        assert r["ADR"] == pytest.approx(70.0)
        assert r["HS%"] == pytest.approx(40.0)
        assert r["accuracy%"] == pytest.approx(20.0)
        assert r["moving 1st shot%"] == pytest.approx(30.0)
        assert r["open W/L"] == "10/15"

    def test_utility_rates(self):
        r = analyze.rates(counters())
        assert r["util thrown/r"] == pytest.approx(1.0)
        assert r["util used% of owned"] == pytest.approx(50.0)
        assert r["flash hit% (>=1 enemy)"] == pytest.approx(25.0)
        assert r["died holding util%"] == pytest.approx(50.0)

    def test_missing_counters_do_not_raise(self):
        """Counters that never incremented are absent from the saved dicts."""
        r = analyze.rates({"rounds": 10})
        assert r["ADR"] == 0
        assert math.isnan(r["K/D"])

    def test_zero_rounds_does_not_divide_by_zero(self):
        r = analyze.rates({})
        assert r["rounds"] == 0
        assert r["ADR"] == 0

    def test_no_flashes_thrown_is_nan_not_zero(self):
        """A player who threw no flashes has no hit rate; 0% would read as 'always missed'."""
        r = analyze.rates(counters(flashes=0, flashes_hit=0))
        assert math.isnan(r["flash hit% (>=1 enemy)"])


class TestScopes:
    def test_scope_rates_read_the_prefixed_counters(self):
        s = counters(h1_rounds=12, h1_kills=10, h1_deaths=5, h1_damage=1200.0)
        h1 = analyze.scope_rates(s, "h1_")
        assert h1["rounds"] == 12
        assert h1["K/D"] == pytest.approx(2.0)
        assert h1["ADR"] == pytest.approx(100.0)

    def test_absent_scope_is_empty_not_an_error(self):
        ot = analyze.scope_rates(counters(), "ot_")
        assert ot["rounds"] == 0

    def test_overall_scope_is_the_unprefixed_one(self):
        assert analyze.scope_rates(counters(), "")["ADR"] == analyze.rates(counters())["ADR"]


class TestPool:
    def test_pool_sums_counters_across_matches(self):
        total = analyze.pool([counters(), counters(rounds=50, kills=25)])
        assert total["rounds"] == 150
        assert total["kills"] == 75

    def test_pooled_rates_are_weighted_by_the_pool(self):
        """Pooling counters then dividing, not averaging per-match rates."""
        total = analyze.pool([counters(rounds=10, damage=1000.0),
                              counters(rounds=90, damage=900.0)])
        assert analyze.rates(total)["ADR"] == pytest.approx(19.0)


def match(stack, players, **extra):
    m = {"demo": "d.dem", "map": "de_anubis", "rounds": 24, "stack": stack, "players": players}
    m.update(extra)
    return m


class TestRoster:
    def test_me_comes_first_then_regulars(self):
        ms = [match(["mirithefish", "a", "b"], {}) for _ in range(4)]
        assert analyze.roster(ms)[0] == analyze.ME
        assert set(analyze.roster(ms)) == {analyze.ME, "a", "b"}

    def test_stand_in_for_one_match_is_excluded(self):
        ms = [match(["mirithefish", "a"], {}) for _ in range(5)]
        ms.append(match(["mirithefish", "standin"], {}))
        assert "standin" not in analyze.roster(ms)
        assert "a" in analyze.roster(ms)

    def test_regular_who_missed_a_match_is_kept(self):
        """The bug this guards: a 5-of-6 teammate used to vanish from the comparison."""
        ms = [match(["mirithefish", "lipton"], {}) for _ in range(5)]
        ms.append(match(["mirithefish", "standin"], {}))
        assert "lipton" in analyze.roster(ms)


class TestRoles:
    def test_awp_share_wins_over_everything(self):
        sig = {"x": analyze.role_signals(counters(awp_kills=20, kills=50)),
               "y": analyze.role_signals(counters())}
        assert analyze.assign_roles(sig)["x"] == "AWP"

    def test_highest_t_side_first_contact_is_the_entry(self):
        sig = {"entry": analyze.role_signals(counters(t_open_won=10, t_open_lost=15)),
               "other": analyze.role_signals(counters(t_open_won=1, t_open_lost=1))}
        assert analyze.assign_roles(sig)["entry"] == "Entry"

    def test_utility_volume_makes_a_support(self):
        sig = {
            "sup": analyze.role_signals(counters(util_thrown=300, smokes=120, flashes=60,
                                                 t_open_won=0, t_open_lost=0)),
            "a": analyze.role_signals(counters(util_thrown=50, smokes=10, flashes=5)),
            "b": analyze.role_signals(counters(util_thrown=40, smokes=8, flashes=4,
                                               t_open_won=20, t_open_lost=20)),
        }
        assert analyze.assign_roles(sig)["sup"] == "Support"

    def test_every_player_gets_a_role(self):
        sig = {n: analyze.role_signals(counters()) for n in "abcde"}
        roles = analyze.assign_roles(sig)
        assert set(roles) == set("abcde")
        assert all(r in analyze.ROLE_KPIS for r in roles.values())


class TestPairMatrix:
    def test_counts_sum_across_matches(self):
        ms = [match(["a", "b"], {}, traded_for={"a": {"b": 2}}),
              match(["a", "b"], {}, traded_for={"a": {"b": 3}})]
        m = analyze.pair_matrix(ms, "traded_for", ["a", "b"])
        assert m["a"]["b"] == 5
        assert m["b"]["a"] == 0

    def test_self_pairs_are_none(self):
        m = analyze.pair_matrix([match(["a"], {})], "traded_for", ["a"])
        assert m["a"]["a"] is None

    def test_distance_is_a_weighted_mean_in_metres(self):
        ms = [match(["a", "b"], {}, prox_sum={"a": {"b": 1000.0}}, prox_n={"a": {"b": 10}}),
              match(["a", "b"], {}, prox_sum={"a": {"b": 3000.0}}, prox_n={"a": {"b": 10}})]
        m = analyze.pair_matrix(ms, "prox_sum", ["a", "b"], mean=True)
        assert m["a"]["b"] == pytest.approx(4000.0 / 20 * analyze.UNIT_M)

    def test_pair_never_sampled_is_none(self):
        m = analyze.pair_matrix([match(["a", "b"], {})], "prox_sum", ["a", "b"], mean=True)
        assert m["a"]["b"] is None
