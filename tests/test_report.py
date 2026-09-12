"""The renderer: it runs unattended against whatever the parser produced, so the cases
that matter are the awkward ones - a missing player, a NaN, a name with an apostrophe.
"""
import pytest

import analyze
import report
from tests.test_analyze import counters


def make_match(names, map_name="de_anubis", rounds=24, **extra):
    players = {n: dict(counters(rounds=rounds, rounds_won=13)) for n in names}
    m = {"demo": "1-abc.dem", "map": map_name, "rounds": rounds,
         "stack": list(names), "players": players}
    m.update(extra)
    return m


@pytest.fixture
def matches():
    names = [analyze.ME, "LipT0N", "Pixellegend"]
    return [make_match(names, "de_anubis"), make_match(names, "de_nuke"),
            make_match(names, "de_inferno")]


class TestRender:
    def test_writes_a_page_per_player_plus_index_and_team(self, matches, tmp_path):
        written = report.render(matches, tmp_path)
        names = {f.name for f in written}
        assert {"index.html", "team.html"} <= names
        assert "player-mirithefish.html" in names
        assert len(names) == 5

    def test_pages_are_not_empty_and_name_the_player(self, matches, tmp_path):
        report.render(matches, tmp_path)
        assert analyze.ME in (tmp_path / "index.html").read_text(encoding="utf-8")
        assert "LipT0N" in (tmp_path / "team.html").read_text(encoding="utf-8")

    def test_no_matches_still_produces_a_page(self, tmp_path):
        """An empty bucket must not leave the served site broken."""
        written = report.render([], tmp_path)
        assert (tmp_path / "index.html").exists()
        assert "No matches" in (tmp_path / "index.html").read_text(encoding="utf-8")

    def test_matches_without_the_owner_are_ignored(self, tmp_path):
        written = report.render([make_match(["someone", "else"])], tmp_path)
        assert [f.name for f in written] == ["index.html"]

    def test_output_stays_far_below_the_configmap_ceiling(self, matches, tmp_path):
        total = sum(f.stat().st_size for f in report.render(matches, tmp_path))
        assert total < 400_000     # the publisher refuses above ~900 KB


class TestEscaping:
    def test_player_names_are_escaped(self, tmp_path):
        """Names come from demos - anyone in the lobby picks their own."""
        hostile = '<script>alert(1)</script>'
        m = make_match([analyze.ME, hostile])
        report.render([m, make_match([analyze.ME, hostile])], tmp_path)
        page = (tmp_path / "team.html").read_text(encoding="utf-8")
        assert "<script>alert(1)</script>" not in page
        assert "&lt;script&gt;" in page

    def test_slug_is_a_safe_filename(self):
        assert report.slug("Pixel/Legend") == "player-pixel-legend.html"
        assert report.slug("../../etc/passwd").startswith("player-")
        assert "/" not in report.slug("a/b")

    def test_slug_of_hostile_name_stays_within_the_directory(self, tmp_path):
        written = report.render([make_match([analyze.ME, "../escape"]),
                                 make_match([analyze.ME, "../escape"])], tmp_path)
        for f in written:
            assert f.parent == tmp_path


class TestFormatting:
    def test_nan_renders_as_a_dash_not_the_word_nan(self):
        assert report.num(float("nan")) == "-"

    def test_none_renders_as_the_given_dash(self):
        assert report.num(None, dash="&mdash;") == "&mdash;"

    def test_numbers_keep_the_requested_precision(self):
        assert report.num(12.3456, 1) == "12.3"
        assert report.num(12.3456, 2) == "12.35"

    def test_a_player_with_no_flashes_does_not_render_nan(self, tmp_path):
        m = make_match([analyze.ME, "Other"])
        for p in m["players"].values():
            p.update(flashes=0, flashes_hit=0)
        report.render([m, make_match([analyze.ME, "Other"])], tmp_path)
        assert "nan" not in (tmp_path / "index.html").read_text(encoding="utf-8").lower()


class TestFaceitStatsPage:
    def stats_rows(self):
        return [{"match_id": "m1", "map": "de_anubis", "score": "13 / 9", "won": True,
                 "finished_at": 1757000000, "nickname": analyze.ME,
                 "stats": {"Kills": "20", "Deaths": "15", "ADR": "78.5",
                           "Headshots %": "45", "Utility Damage": "120"}},
                {"match_id": "m2", "map": "de_nuke", "score": "10 / 13", "won": False,
                 "finished_at": 1757100000, "nickname": analyze.ME,
                 "stats": {"Kills": "12", "Deaths": "18", "ADR": "55.0"}}]

    def test_stats_page_is_written_alongside_the_demo_pages(self, matches, tmp_path):
        written = report.render(matches, tmp_path, stats=self.stats_rows())
        assert "faceit.html" in {f.name for f in written}

    def test_without_demos_the_stats_become_the_front_page(self, tmp_path):
        """The whole point: a nightly trend with no demo uploaded at all."""
        written = report.render([], tmp_path, stats=self.stats_rows())
        page = (tmp_path / "index.html").read_text(encoding="utf-8")
        assert [f.name for f in written] == ["index.html"]
        assert "Recent form" in page
        assert "anubis" in page

    def test_no_demos_and_no_stats_still_renders(self, tmp_path):
        report.render([], tmp_path, stats=[])
        assert "No matches yet" in (tmp_path / "index.html").read_text(encoding="utf-8")

    def test_missing_stat_in_one_match_renders_a_dash_not_nan(self, tmp_path):
        report.render([], tmp_path, stats=self.stats_rows())
        page = (tmp_path / "index.html").read_text(encoding="utf-8")
        assert "nan" not in page.lower()
