"""The API stats path: schema-agnostic extraction, tolerant parsing, aggregation.

The stat keys are undocumented and have changed before, so these tests pin the
behaviour that matters - unknown keys survive, recognised ones are found whatever their
casing, and nothing raises on a payload shaped differently than expected.
"""
import pytest

import faceit_stats


def payload(pid="p1", won_team="t1", stats=None):
    return {
        "rounds": [{
            "match_round": "1",
            "round_stats": {"Map": "de_anubis", "Score": "13 / 9", "Winner": won_team},
            "teams": [
                {"team_id": "t1", "players": [
                    {"player_id": pid, "nickname": "mirithefish",
                     "player_stats": stats if stats is not None else
                     {"Kills": "20", "Deaths": "15", "ADR": "78.5", "Headshots %": "45",
                      "Utility Damage": "120", "Some New Stat": "7"}}]},
                {"team_id": "t2", "players": [
                    {"player_id": "other", "nickname": "enemy", "player_stats": {"Kills": "9"}}]},
            ],
        }]
    }


class TestExtract:
    def test_picks_only_the_requested_player(self):
        rows = faceit_stats.extract(payload(), "p1")
        assert len(rows) == 1
        assert rows[0]["nickname"] == "mirithefish"

    def test_reads_map_and_score(self):
        row = faceit_stats.extract(payload(), "p1")[0]
        assert row["map"] == "de_anubis"
        assert row["score"] == "13 / 9"

    def test_win_is_decided_by_the_winning_team_id(self):
        assert faceit_stats.extract(payload(won_team="t1"), "p1")[0]["won"] is True
        assert faceit_stats.extract(payload(won_team="t2"), "p1")[0]["won"] is False

    def test_unknown_stats_are_kept_verbatim(self):
        """The schema is undocumented: dropping unrecognised keys would lose data we
        cannot get back later."""
        row = faceit_stats.extract(payload(), "p1")[0]
        assert row["stats"]["Some New Stat"] == "7"

    def test_absent_player_yields_nothing(self):
        assert faceit_stats.extract(payload(), "nobody") == []

    @pytest.mark.parametrize("broken", [{}, {"rounds": []}, {"rounds": [{}]},
                                        {"rounds": [{"teams": None}]},
                                        {"rounds": [{"teams": [{"players": None}]}]}])
    def test_malformed_payloads_do_not_raise(self, broken):
        assert faceit_stats.extract(broken, "p1") == []


class TestNumbers:
    @pytest.mark.parametrize("raw,expected", [
        ("20", 20.0), (20, 20.0), ("78.5", 78.5), ("45%", 45.0), ("1,234", 1234.0),
        (" 12 ", 12.0),
    ])
    def test_values_arrive_as_strings_and_parse(self, raw, expected):
        assert faceit_stats.as_number(raw) == expected

    @pytest.mark.parametrize("raw", ["", "n/a", None, {}, "--"])
    def test_unparseable_values_are_none_not_zero(self, raw):
        """Zero would silently drag every average down."""
        assert faceit_stats.as_number(raw) is None


class TestCurated:
    def test_finds_keys_regardless_of_case(self):
        got = dict(faceit_stats.curated({"kills": "5", "adr": "70"}))
        assert got["Kills"] == 5.0
        assert got["ADR"] == 70.0

    def test_uses_the_key_faceit_actually_sends(self):
        """Confirmed live: the key is "K/D Ratio", not "K/D"."""
        assert dict(faceit_stats.curated({"K/D Ratio": "1.2"}))["K/D"] == 1.2

    def test_missing_stats_are_simply_absent(self):
        assert dict(faceit_stats.curated({})) == {}


class TestAverages:
    def test_numeric_stats_are_averaged(self):
        rows = [{"stats": {"ADR": "80"}, "won": True}, {"stats": {"ADR": "60"}, "won": False}]
        avg = faceit_stats.averages(rows)
        assert avg["ADR"] == pytest.approx(70.0)
        assert avg["_maps"] == 2
        assert avg["_win_rate"] == pytest.approx(50.0)

    def test_non_numeric_stats_are_skipped(self):
        avg = faceit_stats.averages([{"stats": {"Result": "win", "ADR": "80"}}])
        assert "Result" not in avg
        assert avg["ADR"] == 80.0

    def test_stats_present_in_only_some_matches_average_over_those(self):
        rows = [{"stats": {"Utility Damage": "100"}}, {"stats": {}}]
        assert faceit_stats.averages(rows)["Utility Damage"] == pytest.approx(100.0)

    def test_empty_input(self):
        assert faceit_stats.averages([])["_maps"] == 0


class TestRealSchema:
    """Keys confirmed from a live run, so a rename shows up as a test failure."""

    LIVE = {"ADR": "41.2", "K/D Ratio": "0.38", "Headshots %": "17", "Utility Count": "9",
            "Utility Usage per Round": "0.5", "Utility Damage": "75", "Flash Count": "0",
            "Flash Successes": "0", "Flash Success Rate per Match": "0",
            "Enemies Flashed": "0", "Entry Count": "3", "Entry Wins": "0",
            "Match Entry Success Rate": "0", "Match Entry Rate": "0.17",
            "First Kills": "0", "Clutch Kills": "0", "1v1Wins": "0",
            "Utility Damage per Round in a Match": "4.2",
            "Enemies Flashed per Round in a Match": "0"}

    def test_headline_metrics_all_resolve(self):
        got = dict(faceit_stats.curated(self.LIVE))
        missing = [l for l in faceit_stats.HEADLINE if l not in got]
        assert not missing, missing

    def test_fraction_rates_are_scaled_to_percent(self):
        got = dict(faceit_stats.curated({"Match Entry Rate": "0.17"}))
        assert got["Entries / round"] == pytest.approx(17.0)

    def test_percentages_already_in_percent_are_untouched(self):
        assert dict(faceit_stats.curated({"Headshots %": "17"}))["HS %"] == 17.0

    def test_utility_and_flash_volume_are_available(self):
        """These were the metrics I assumed the API lacked; it has them."""
        got = dict(faceit_stats.curated(self.LIVE))
        assert got["Utility thrown"] == 9.0
        assert got["Flashes thrown"] == 0.0
