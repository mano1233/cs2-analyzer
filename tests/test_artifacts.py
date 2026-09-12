"""The canonical artifacts: layout, provenance, and the never-merge rule.

These files are the only thing that must survive - the rollup and the pages are both
rebuilt from them - so the tests are about shape and safety rather than numbers.
"""
import pytest

import analyze
import artifacts
from tests.test_analyze import counters, match as make_match_dict


@pytest.fixture
def match():
    players = {analyze.ME: dict(counters(rounds=24, rounds_won=13, t_rounds=12, ct_rounds=12)),
               "LipT0N": dict(counters(rounds=24, rounds_won=13, t_rounds=12, ct_rounds=12)),
               "enemy1": dict(counters(rounds=24, rounds_won=11, t_rounds=12, ct_rounds=12))}
    return {
        "demo": "1-abc.dem", "map": "de_anubis", "rounds": 24,
        "stack": [analyze.ME, "LipT0N"], "players": players,
        "steamids": {analyze.ME: "76561198059143085", "LipT0N": "76561198830705343",
                     "enemy1": "76561198000000001"},
        "started_side": {analyze.ME: analyze.T_SIDE, "LipT0N": analyze.T_SIDE,
                         "enemy1": analyze.CT_SIDE},
        "round_table": [{"round": 1, "half": 1, "winner": 2, "opening_kill_team": 2,
                         "opening_killer": analyze.ME, "opening_victim": "enemy1",
                         "opening_time": 20.0, "planted": True, "plant_site": 753,
                         "plant_x": 1200.0, "plant_y": 1898.0, "plant_z": -191.0,
                         "plant_time": 40.0, "defused": False,
                         "t_equip": 21000.0, "t_bucket": "full",
                         "ct_equip": 4000.0, "ct_bucket": "eco"}],
        "traded_for": {analyze.ME: {"LipT0N": 2}},
        "flash_conv": {}, "prox_sum": {}, "prox_n": {},
    }


class TestLayout:
    def test_writes_metadata_teams_and_one_file_per_player(self, match):
        files = artifacts.build(match, "1-abc", "demos/1-abc.dem.zst")
        assert "metadata.json" in files and "teams.json" in files
        assert sum(1 for k in files if k.startswith("players/")) == 3

    def test_player_paths_are_confined_to_the_match_prefix(self, match):
        match["players"]["../../etc/passwd"] = dict(counters())
        match["steamids"]["../../etc/passwd"] = "1"
        files = artifacts.build(match, "1-abc", "demos/1-abc.dem.zst")
        for key in files:
            assert ".." not in key
            assert key.count("/") <= 1

    def test_prefix_is_date_then_match(self):
        assert artifacts.prefix_for("1-abc", "2026-09-11") == "matches/2026-09-11/1-abc/"

    @pytest.mark.parametrize("nickname,expected", [
        ("mirithefish", "mirithefish"), ("LipT0N", "lipt0n"),
        ("Pixel/Legend", "pixel-legend"), ("../escape", "escape"), ("", "player"),
    ])
    def test_slugs(self, nickname, expected):
        assert artifacts.slug(nickname) == expected


class TestMetadata:
    def test_faceit_link_only_for_faceit_matches(self, match):
        faceit = artifacts.build(match, "1-abc", "demos/1-abc.dem.zst")["metadata.json"]
        assert faceit["source"] == "faceit"
        assert faceit["demo"]["faceit_url"].endswith("/room/1-abc")

        match["demo"] = "match730_003842202518796894628_2030511689_273.dem"
        prem = artifacts.build(match, "match730_x", "demos/match730_x.dem")["metadata.json"]
        assert prem["source"] == "premier"
        assert prem["demo"]["faceit_url"] is None

    def test_premier_ids_are_parsed_from_the_filename(self, match):
        match["demo"] = "match730_003842202518796894628_2030511689_273.dem"
        meta = artifacts.build(match, "match730_x", "demos/match730_x.dem")["metadata.json"]
        assert meta["premier_ids"] == {"matchid": "003842202518796894628",
                                       "outcomeid": "2030511689", "token": "273"}

    def test_faceit_matches_have_no_premier_ids(self, match):
        assert artifacts.build(match, "1-abc", "demos/1-abc.dem.zst")["metadata.json"]["premier_ids"] is None

    def test_steamids_survive_as_strings(self, match):
        meta = artifacts.build(match, "1-abc", "demos/1-abc.dem.zst")["metadata.json"]
        ids = [p["steamid64"] for team in meta["teams"] for p in team["players"]]
        assert "76561198059143085" in ids
        assert all(isinstance(i, str) for i in ids if i)

    def test_our_stack_is_marked(self, match):
        meta = artifacts.build(match, "1-abc", "demos/1-abc.dem.zst")["metadata.json"]
        assert any(t["is_our_stack"] for t in meta["teams"])
        assert any(not t["is_our_stack"] for t in meta["teams"])

    def test_the_two_teams_are_actually_split(self, match):
        """Sides swap at the half, so round counts cannot separate them - round 1 can.
        This caught a bug that put all ten players on one team."""
        meta = artifacts.build(match, "1-abc", "demos/1-abc.dem.zst")["metadata.json"]
        assert len(meta["teams"]) == 2
        sizes = sorted(len(t["players"]) for t in meta["teams"])
        assert sizes == [1, 2]      # fixture has 2 on our side, 1 opponent
        assert {t["started_as"] for t in meta["teams"]} == {"T", "CT"}

    def test_provenance_records_the_image(self, match):
        meta = artifacts.build(match, "1-abc", "demos/1-abc.dem.zst", image="0.12.0")["metadata.json"]
        assert meta["parsed_with"]["image"] == "0.12.0"
        assert meta["schema_version"] == artifacts.SCHEMA_VERSION

    def test_demo_availability_is_unknown_not_assumed(self, match):
        meta = artifacts.build(match, "1-abc", "demos/1-abc.dem.zst")["metadata.json"]
        assert meta["demo"]["available"] is None

    @pytest.mark.parametrize("finished,mtime,expected", [
        (1757000000, None, "2025-09-04"), (None, 1757000000, "2025-09-04"),
    ])
    def test_date_prefers_the_api_finish_time(self, match, finished, mtime, expected):
        assert artifacts.match_date(match, finished, mtime) == expected


class TestPlayerFiles:
    def test_sources_are_namespaced_not_merged(self, match):
        rows = [{"match_id": "1-abc", "nickname": analyze.ME,
                 "stats": {"ADR": "78.5", "Utility Count": "9"}}]
        files = artifacts.build(match, "1-abc", "demos/1-abc.dem.zst", stats_rows=rows)
        me = files["players/mirithefish.json"]
        demo_adr = analyze.rates(match["players"][analyze.ME])["ADR"]
        assert me["demo"]["rates"]["ADR"] == pytest.approx(demo_adr)
        assert me["faceit"]["curated"]["ADR"] == 78.5       # different number, kept apart
        assert me["faceit"]["curated"]["ADR"] != pytest.approx(demo_adr)
        assert "ADR" not in me                              # never flattened together

    def test_players_without_api_stats_get_nulls(self, match):
        files = artifacts.build(match, "1-abc", "demos/1-abc.dem.zst")
        assert files["players/lipt0n.json"]["faceit"]["stats"] is None

    def test_every_scope_is_present(self, match):
        me = artifacts.build(match, "1-abc", "demos/1-abc.dem.zst")["players/mirithefish.json"]
        assert set(me["demo"]["scopes"]) == {"all", "t", "ct", "h1", "h2", "ot"}


class TestTeamsFile:
    def test_carries_round_table_metrics_and_thresholds(self, match):
        teams = artifacts.build(match, "1-abc", "demos/1-abc.dem.zst")["teams.json"]
        assert teams["round_metrics"]["rounds"] == 1
        assert teams["round_table"][0]["planted"] is True
        assert teams["economy_thresholds"] == {"eco_below": analyze.ECO_MAX,
                                               "full_above": analyze.FORCE_MAX}

    def test_sites_come_from_coordinates(self, match):
        teams = artifacts.build(match, "1-abc", "demos/1-abc.dem.zst")["teams.json"]
        assert teams["sites"]["de_anubis"][0]["plants"] == 1

    def test_pair_matrices_are_carried_verbatim(self, match):
        teams = artifacts.build(match, "1-abc", "demos/1-abc.dem.zst")["teams.json"]
        assert teams["pairs"]["traded_for"][analyze.ME]["LipT0N"] == 2


def test_everything_is_json_serialisable(match):
    import json
    files = artifacts.build(match, "1-abc", "demos/1-abc.dem.zst")
    for name, obj in files.items():
        json.loads(json.dumps(obj, default=float))
