"""The derived tables: stable metric names, tidy shape, and no merged sources."""
import io

import pyarrow.parquet as pq
import pytest

import analyze
import rollup
from tests.test_analyze import counters


@pytest.fixture
def match():
    return {
        "demo": "1-abc.dem", "source_key": "demos/1-abc.dem.zst", "map": "de_anubis",
        "rounds": 24, "stack": [analyze.ME, "LipT0N"],
        "players": {analyze.ME: dict(counters(rounds=24, t_rounds=12, ct_rounds=12,
                                              h1_rounds=12, h1_damage=900.0, h1_kills=8, h1_deaths=6)),
                    "LipT0N": dict(counters(rounds=24, t_rounds=12, ct_rounds=12)),
                    "enemy": dict(counters(rounds=24))},
        "started_side": {analyze.ME: analyze.T_SIDE, "LipT0N": analyze.T_SIDE,
                         "enemy": analyze.CT_SIDE},
        "steamids": {}, "round_table": [
            {"round": 1, "half": 1, "winner": 2, "opening_kill_team": 2,
             "opening_killer": analyze.ME, "opening_victim": "enemy", "opening_time": 12.0,
             "planted": True, "plant_site": 753, "plant_x": 1.0, "plant_y": 2.0,
             "plant_z": 3.0, "plant_time": 30.0, "defused": False,
             "t_equip": 25000.0, "t_bucket": "full", "ct_equip": 3000.0, "ct_bucket": "eco"}],
    }


def read(blob):
    return pq.read_table(io.BytesIO(blob)).to_pylist()


class TestMetricKeys:
    @pytest.mark.parametrize("label,expected", [
        ("moving 1st shot%", "moving_1st_shot_pct"),
        ("util thrown/r", "util_thrown_per_round"),
        ("flash->kill", "flash_to_kill"),
        ("flash hit% (>=1 enemy)", "flash_hit_pct_at_least1_enemy"),
        ("K/D", "k_d"),
        ("ADR", "adr"),
        ("avg util time T (s)", "avg_util_time_t_s"),
    ])
    def test_labels_become_sql_safe_names(self, label, expected):
        assert rollup.metric_key(label) == expected

    def test_keys_are_stable_across_calls(self):
        assert rollup.metric_key("HS%") == rollup.metric_key("hs%")


class TestPlayerRows:
    def test_only_tracked_players_appear(self, match):
        rows = rollup.player_rows(match, "1-abc", "2026-09-11")
        assert {r["player"] for r in rows} == {analyze.ME, "LipT0N"}

    def test_team_comes_from_the_round_one_side(self, match):
        rows = rollup.player_rows(match, "1-abc", "2026-09-11")
        assert {r["team"] for r in rows} == {"T"}

    def test_scopes_with_no_rounds_are_omitted(self, match):
        """An absent half must not become a row of zeros."""
        scopes = {r["scope"] for r in rollup.player_rows(match, "1-abc", "2026-09-11")}
        assert "ot" not in scopes
        assert {"all", "t", "ct", "h1"} <= scopes

    def test_labels_like_open_w_l_are_dropped(self, match):
        """"10/15" is a label, not a measurement - a value column cannot hold it."""
        metrics = {r["metric"] for r in rollup.player_rows(match, "1-abc", "2026-09-11")}
        assert "open_w_l" not in metrics

    def test_nan_metrics_are_omitted_not_stored_as_null(self, match):
        match["players"][analyze.ME].update(flashes=0, flashes_hit=0)
        rows = rollup.player_rows(match, "1-abc", "2026-09-11")
        flash = [r for r in rows if r["metric"].startswith("flash_hit_pct") and r["player"] == analyze.ME]
        assert flash == []

    def test_api_stats_are_a_separate_stat_source(self, match):
        stats = [{"match_id": "1-abc", "nickname": analyze.ME,
                  "stats": {"ADR": "78.5", "Utility Count": "9"}}]
        rows = rollup.player_rows(match, "1-abc", "2026-09-11", stats_rows=stats)
        demo_adr = [r for r in rows if r["metric"] == "adr" and r["stat_source"] == "demo"]
        api_adr = [r for r in rows if r["metric"] == "adr" and r["stat_source"] == "faceit"]
        assert demo_adr and api_adr
        assert api_adr[0]["value"] == 78.5
        assert demo_adr[0]["value"] != api_adr[0]["value"]

    def test_counters_are_included_alongside_rates(self, match):
        metrics = {r["metric"] for r in rollup.player_rows(match, "1-abc", "2026-09-11")}
        assert "count_kills" in metrics      # how much
        assert "k_d" in metrics              # how good

    def test_unscoped_metrics_are_queryable_too(self, match):
        """scope_rates only carries what splits by side or half, so the utility family
        reached the parquet as raw counters and nothing else. These are the numbers the
        practice is aimed at - they have to be answerable in SQL, not just on a page."""
        match["players"][analyze.ME].update(death_nearest_sum=24000.0, death_nearest_n=40)
        metrics = {r["metric"] for r in rollup.player_rows(match, "1-abc", "2026-09-11")
                   if r["scope"] == "all" and r["stat_source"] == "demo"}
        for expected in ("nearest_mate_at_death_m", "flash_to_kill_flash", "util_dmg_per_round",
                         "util_used_pct_of_owned", "died_holding_util_pct",
                         "util_before_1st_kill_pct"):
            assert expected in metrics, expected

    def test_the_side_scopes_stay_side_shaped(self, match):
        """rates() is unscoped, so merging it into t_/ct_ would silently attribute
        whole-match numbers to one side."""
        rows = rollup.player_rows(match, "1-abc", "2026-09-11")
        t_metrics = {r["metric"] for r in rows if r["scope"] == "t" and r["stat_source"] == "demo"}
        assert "nearest_mate_at_death_m" not in t_metrics

    def test_one_number_is_not_published_under_two_names(self, match):
        """rates() and scope_rates() both compute flash hit rate; they must agree on
        the label, or a query gets two metrics that are the same measurement."""
        rows = [r for r in rollup.player_rows(match, "1-abc", "2026-09-11")
                if r["scope"] == "all" and r["player"] == analyze.ME
                and r["metric"].startswith("flash_hit_pct")]
        assert len({r["metric"] for r in rows}) == 1


class TestMatchRows:
    def test_team_metrics_are_present(self, match):
        metrics = {r["metric"] for r in rollup.match_rows(match, "1-abc", "2026-09-11")}
        assert "plants" in metrics
        assert any(m.startswith("t_opening_conversion") for m in metrics)

    def test_non_numeric_entries_are_skipped(self, match):
        """round_metrics carries a sites dict; a tidy table holds numbers only."""
        rows = rollup.match_rows(match, "1-abc", "2026-09-11")
        assert all(isinstance(r["value"], float) for r in rows)


class TestParquet:
    def test_round_trips_with_the_declared_schema(self, match):
        players, matches = rollup.build([match])
        prows, mrows = read(players), read(matches)
        assert prows and mrows
        assert set(prows[0]) == {f.name for f in rollup.SCHEMA}
        assert set(mrows[0]) == {f.name for f in rollup.MATCH_SCHEMA}

    def test_empty_input_still_produces_a_valid_file(self):
        """A query against a fresh bucket should return zero rows, not fail."""
        players, matches = rollup.build([])
        assert read(players) == []
        assert read(matches) == []

    def test_values_are_floats_not_strings(self, match):
        rows = read(rollup.build([match])[0])
        assert all(isinstance(r["value"], float) for r in rows)
