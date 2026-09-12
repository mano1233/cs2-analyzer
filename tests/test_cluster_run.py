"""The CronJob entrypoint: the trend row it appends, and the guards around publishing.

The parts that talk to R2, FACEIT and the Kubernetes API are not exercised here - they
are one call each and mocking them tests the mock. What is worth testing is the logic
that decides what gets written and when publishing is refused.
"""
import pathlib

import pytest

import analyze
import cluster_run
from tests.test_analyze import counters


@pytest.fixture
def match():
    return {"demo": "1-abc.dem", "map": "de_anubis", "rounds": 24, "stack": [analyze.ME],
            "players": {analyze.ME: dict(counters(rounds=24, rounds_won=13))}}


class TestTrendRow:
    def test_row_has_every_declared_field(self, match):
        row = cluster_run.row_for(match, "demos/1-abc.dem.zst")
        assert set(row) == set(cluster_run.TREND_FIELDS)

    def test_result_is_rounds_won_to_rounds_lost(self, match):
        assert cluster_run.row_for(match, "k")["result"] == "13-11"

    @pytest.mark.parametrize("key,expected", [
        ("demos/1-abc.dem.zst", "faceit"),
        ("demos/match730_00384220.dem", "premier"),
    ])
    def test_source_comes_from_the_key(self, match, key, expected):
        assert cluster_run.row_for(match, key)["source"] == expected

    def test_missing_owner_yields_no_row(self, match):
        match["players"] = {"someone": dict(counters())}
        assert cluster_run.row_for(match, "k") is None

    def test_nan_becomes_blank_not_the_string_nan(self, match):
        """A blank cell is honest in a CSV; "nan" poisons anything that reads it."""
        match["players"][analyze.ME].update(flashes=0, flashes_hit=0)
        assert cluster_run.row_for(match, "k")["flash_hit_pct"] == ""

    def test_demo_column_is_the_file_not_the_full_key(self, match):
        assert cluster_run.row_for(match, "demos/1-abc.dem.zst")["demo"] == "1-abc.dem.zst"


class TestPublishGuards:
    def test_no_configmap_configured_means_render_only(self, match, tmp_path, monkeypatch):
        monkeypatch.setattr(cluster_run, "REPORT_CONFIGMAP", "")
        monkeypatch.setattr(cluster_run, "REPORT_DIR", tmp_path)
        cluster_run.publish_report([match])
        assert (tmp_path / "index.html").exists()      # rendered
        # nothing raised despite no API server in reach

    def test_oversized_report_is_refused(self, match, tmp_path, monkeypatch):
        """ConfigMaps cap near 1 MiB; a too-large PATCH must not be attempted."""
        monkeypatch.setattr(cluster_run, "REPORT_CONFIGMAP", "cs2-report")
        monkeypatch.setattr(cluster_run, "REPORT_DIR", tmp_path)
        monkeypatch.setattr(cluster_run, "SA", pathlib.Path(tmp_path / "no-sa"))

        big = tmp_path / "index.html"
        def fake_render(results, out_dir):
            big.parent.mkdir(parents=True, exist_ok=True)
            big.write_text("x" * 1_000_000, encoding="utf-8")
            return [big]
        monkeypatch.setattr(cluster_run.report, "render", fake_render)

        logged = []
        monkeypatch.setattr(cluster_run, "log", logged.append)
        cluster_run.publish_report([match])
        assert any("too close to the ConfigMap limit" in m for m in logged)

    def test_missing_service_account_is_logged_not_fatal(self, match, tmp_path, monkeypatch):
        """Running the image outside the cluster must not crash the run."""
        monkeypatch.setattr(cluster_run, "REPORT_CONFIGMAP", "cs2-report")
        monkeypatch.setattr(cluster_run, "REPORT_DIR", tmp_path)
        monkeypatch.setattr(cluster_run, "SA", pathlib.Path(tmp_path / "absent"))
        logged = []
        monkeypatch.setattr(cluster_run, "log", logged.append)
        cluster_run.publish_report([match])
        assert any("no service account" in m for m in logged)


class TestConfig:
    def test_window_is_three_weeks_by_default(self):
        assert cluster_run.WINDOW_DAYS == 21

    def test_trend_fields_match_the_row_builder(self, match):
        row = cluster_run.row_for(match, "k")
        assert list(row) == cluster_run.TREND_FIELDS


class TestDownloadsGate:
    def test_no_downloads_token_skips_the_fetch_entirely(self, monkeypatch):
        """Data API demo URLs are private; without the Downloads token there is nothing
        to try, so the run must not walk the whole window failing per match."""
        monkeypatch.setattr(cluster_run, "API_KEY", "key")
        monkeypatch.setattr(cluster_run, "DOWNLOADS_TOKEN", "")
        called = []
        monkeypatch.setattr(cluster_run, "faceit_history", lambda: called.append(1) or [])
        logged = []
        monkeypatch.setattr(cluster_run, "log", logged.append)
        assert cluster_run.fetch_faceit({}) == []
        assert not called
        assert any("FACEIT_DOWNLOADS_TOKEN" in m for m in logged)
