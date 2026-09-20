"""The CronJob entrypoint: the trend row it appends, and the guards around publishing.

The parts that talk to R2, FACEIT and the Kubernetes API are not exercised here - they
are one call each and mocking them tests the mock. What is worth testing is the logic
that decides what gets written and when publishing is refused.
"""
import csv
import io
import json
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
        def fake_render(results, out_dir, stats=()):
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


class TestConfigMapReplacesRatherThanMerges:
    """A merge patch only adds and overwrites, so a page we stop generating stays
    served forever, frozen at whatever it said the day it was dropped."""

    def publish(self, match, tmp_path, monkeypatch, pages=("index.html",)):
        sa = tmp_path / "sa"
        sa.mkdir()
        (sa / "namespace").write_text("cs2")
        (sa / "token").write_text("t0ken")
        (sa / "ca.crt").write_text("not-a-real-pem")
        monkeypatch.setattr(cluster_run, "REPORT_CONFIGMAP", "cs2-report")
        monkeypatch.setattr(cluster_run, "REPORT_DIR", tmp_path / "out")
        monkeypatch.setattr(cluster_run, "SA", sa)
        monkeypatch.setattr(cluster_run.ssl, "create_default_context", lambda cafile=None: None)

        def fake_render(results, out_dir, stats=()):
            d = pathlib.Path(out_dir)
            d.mkdir(parents=True, exist_ok=True)
            written = []
            for name in pages:
                (d / name).write_text("<title>%s</title>" % name, encoding="utf-8")
                written.append(d / name)
            return written

        monkeypatch.setattr(cluster_run.report, "render", fake_render)

        sent = {}

        class Resp:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake_open(req, timeout=None, context=None):
            sent["method"] = req.get_method()
            sent["type"] = req.get_header("Content-type")
            sent["body"] = json.loads(req.data.decode())
            return Resp()

        monkeypatch.setattr(cluster_run.urllib.request, "urlopen", fake_open)
        cluster_run.publish_report([match])
        return sent

    def test_the_whole_data_map_is_replaced(self, match, tmp_path, monkeypatch):
        sent = self.publish(match, tmp_path, monkeypatch, pages=("index.html", "player-a.html"))
        assert sent["method"] == "PATCH"
        assert sent["type"] == "application/json-patch+json"
        assert [op["op"] for op in sent["body"]] == ["add"]
        assert sent["body"][0]["path"] == "/data"
        assert set(sent["body"][0]["value"]) == {"index.html", "player-a.html"}

    def test_a_page_we_no_longer_render_is_not_in_the_payload(self, match, tmp_path, monkeypatch):
        """team.html outlived the redesign that deleted it; replacing /data is what
        stops the next dropped page doing the same."""
        sent = self.publish(match, tmp_path, monkeypatch, pages=("index.html",))
        assert "team.html" not in sent["body"][0]["value"]


class TestDemoKeys:
    """What the run is willing to hand to the parser."""

    def listing(self, monkeypatch, keys):
        monkeypatch.setattr(cluster_run, "list_keys", lambda prefix: keys)

    def test_the_folder_marker_is_not_a_demo(self, monkeypatch):
        """Creating the folder in the R2 console leaves a zero-byte "demos/" object.
        The parser panics in Rust on an empty file, which killed the whole run."""
        self.listing(monkeypatch, ["demos/", "demos/1-abc.dem.zst"])
        assert cluster_run.demo_keys() == ["demos/1-abc.dem.zst"]

    def test_strays_under_the_prefix_are_ignored(self, monkeypatch):
        self.listing(monkeypatch, ["demos/notes.txt", "demos/.keep", "demos/1-abc.dem"])
        assert cluster_run.demo_keys() == ["demos/1-abc.dem"]

    @pytest.mark.parametrize("name", ["1-abc.dem", "1-abc.dem.zst", "1-abc.dem.bz2"])
    def test_every_demo_form_we_upload_is_accepted(self, monkeypatch, name):
        self.listing(monkeypatch, ["demos/" + name])
        assert cluster_run.demo_keys() == ["demos/" + name]


class TestOneBadDemo:
    """A demo the parser cannot read must cost that demo, not the run."""

    def run_with(self, monkeypatch, tmp_path, failure):
        written = {}
        monkeypatch.setattr(cluster_run, "SCRATCH", tmp_path)
        monkeypatch.setattr(cluster_run, "get_json", lambda key, default: default)
        monkeypatch.setattr(cluster_run, "put_json", lambda key, obj: written.__setitem__(key, obj))
        monkeypatch.setattr(cluster_run, "fetch_faceit", lambda state: None)
        monkeypatch.setattr(cluster_run, "sync_stats", lambda state: [])
        monkeypatch.setattr(cluster_run, "demo_keys", lambda: ["demos/bad.dem", "demos/good.dem"])
        monkeypatch.setattr(cluster_run, "append_trend", lambda rows: None)
        monkeypatch.setattr(cluster_run, "write_rollup", lambda r, s: None)
        monkeypatch.setattr(cluster_run, "publish_report", lambda r, s: None)
        monkeypatch.setattr(cluster_run, "write_artifacts", lambda m, k, s: None)

        class FakeS3:
            def download_file(self, bucket, key, path):
                pathlib.Path(path).write_bytes(b"not really a demo")

        monkeypatch.setattr(cluster_run, "s3", lambda: FakeS3())

        def parse(dem):
            if "bad" in str(dem):
                raise failure
            return {"demo": dem.name, "map": "de_nuke", "rounds": 24, "stack": [],
                    "players": {}}

        monkeypatch.setattr(cluster_run.analyze, "analyze", parse)
        return cluster_run.main(), written

    def test_a_rust_panic_does_not_end_the_run(self, monkeypatch, tmp_path):
        """demoparser2 panics rather than raising on a malformed demo, and pyo3 hands
        that back as a PanicException - which is a BaseException, not an Exception."""
        class PanicException(BaseException):
            pass

        code, written = self.run_with(monkeypatch, tmp_path, PanicException("range end index 16"))
        assert code == 0
        assert len(written[cluster_run.RESULTS_KEY]) == 1      # the good one still landed
        assert written[cluster_run.STATE_KEY]["demos"] == ["demos/good.dem"]

    def test_an_ordinary_error_behaves_the_same(self, monkeypatch, tmp_path):
        code, written = self.run_with(monkeypatch, tmp_path, ValueError("corrupt"))
        assert code == 0
        assert len(written[cluster_run.RESULTS_KEY]) == 1

    def test_a_stop_signal_is_still_a_stop_signal(self, monkeypatch, tmp_path):
        """Catching BaseException must not swallow the interpreter shutting down."""
        with pytest.raises(KeyboardInterrupt):
            self.run_with(monkeypatch, tmp_path, KeyboardInterrupt())


class TestDemoDeletion:
    """The only thing that removes data from the bucket. Off unless asked for, and
    never on a demo whose canonical artifacts did not land."""

    def run_with(self, monkeypatch, tmp_path, *, enabled, archived=True):
        deleted, written = [], {}
        monkeypatch.setattr(cluster_run, "SCRATCH", tmp_path)
        monkeypatch.setattr(cluster_run, "DELETE_PARSED_DEMOS", enabled)
        monkeypatch.setattr(cluster_run, "get_json", lambda key, default: default)
        monkeypatch.setattr(cluster_run, "put_json", lambda key, obj: written.__setitem__(key, obj))
        monkeypatch.setattr(cluster_run, "fetch_faceit", lambda state: None)
        monkeypatch.setattr(cluster_run, "sync_stats", lambda state: [])
        monkeypatch.setattr(cluster_run, "demo_keys", lambda: ["demos/a.dem"])
        monkeypatch.setattr(cluster_run, "append_trend", lambda rows: None)
        monkeypatch.setattr(cluster_run, "write_rollup", lambda r, s: None)
        monkeypatch.setattr(cluster_run, "publish_report", lambda r, s: None)
        monkeypatch.setattr(cluster_run, "write_artifacts", lambda m, k, s: archived)

        class FakeS3:
            def download_file(self, bucket, key, path):
                pathlib.Path(path).write_bytes(b"demo")

            def delete_object(self, Bucket, Key):
                deleted.append(Key)

        monkeypatch.setattr(cluster_run, "s3", lambda: FakeS3())
        monkeypatch.setattr(cluster_run.analyze, "analyze", lambda dem: {
            "demo": dem.name, "map": "de_nuke", "rounds": 24, "stack": [], "players": {}})
        assert cluster_run.main() == 0
        return deleted

    def test_demos_are_kept_by_default(self, monkeypatch, tmp_path):
        """A demo that is gone can never be re-parsed, so every future metric would
        start from the day it shipped instead of applying to history."""
        assert self.run_with(monkeypatch, tmp_path, enabled=False) == []

    def test_the_toggle_deletes_the_parsed_demo(self, monkeypatch, tmp_path):
        assert self.run_with(monkeypatch, tmp_path, enabled=True) == ["demos/a.dem"]

    def test_a_failed_artifact_write_keeps_the_demo(self, monkeypatch, tmp_path):
        """Otherwise the only copy of the match is deleted along with the demo."""
        assert self.run_with(monkeypatch, tmp_path, enabled=True, archived=False) == []


class TestTrendIsKeyed:
    def row(self, demo, **over):
        return dict({f: "" for f in cluster_run.TREND_FIELDS}, demo=demo, **over)

    def test_first_write_gets_a_header(self):
        out = cluster_run.merge_trend("", [self.row("a.dem", map="de_nuke")])
        assert out.splitlines()[0] == ",".join(cluster_run.TREND_FIELDS)
        assert len(out.strip().splitlines()) == 2

    def test_a_new_demo_is_added(self):
        first = cluster_run.merge_trend("", [self.row("a.dem")])
        out = cluster_run.merge_trend(first, [self.row("b.dem")])
        assert len(out.strip().splitlines()) == 3

    def test_reparsing_corrects_the_row_instead_of_adding_one(self):
        """Otherwise a metric fix leaves two rows making different claims about the
        same match, and the trend line double-counts it."""
        first = cluster_run.merge_trend("", [self.row("a.dem", adr="70.0")])
        out = cluster_run.merge_trend(first, [self.row("a.dem", adr="81.5")])
        body = out.strip().splitlines()
        assert len(body) == 2
        assert "81.5" in body[1] and "70.0" not in out

    def test_order_is_stable_so_the_trend_reads_oldest_first(self):
        out = cluster_run.merge_trend("", [self.row("a.dem"), self.row("b.dem")])
        out = cluster_run.merge_trend(out, [self.row("a.dem", map="de_nuke")])
        demos = [r["demo"] for r in csv.DictReader(io.StringIO(out))]
        assert demos == ["a.dem", "b.dem"]


class TestResultsAreKeyed:
    """Re-parsing a demo replaces its entry. Appending left results.json holding
    three schema generations at once, which made the aggregates disagree."""

    def test_a_reparse_replaces_rather_than_duplicates(self, monkeypatch, tmp_path):
        stored = [{"demo": "a.dem", "source_key": "demos/a.dem", "map": "de_nuke",
                   "rounds": 24, "stack": [], "players": {}}]
        written = {}
        monkeypatch.setattr(cluster_run, "SCRATCH", tmp_path)
        monkeypatch.setattr(cluster_run, "get_json",
                            lambda key, default: stored if key == cluster_run.RESULTS_KEY else default)
        monkeypatch.setattr(cluster_run, "put_json", lambda key, obj: written.__setitem__(key, obj))
        monkeypatch.setattr(cluster_run, "fetch_faceit", lambda state: None)
        monkeypatch.setattr(cluster_run, "sync_stats", lambda state: [])
        monkeypatch.setattr(cluster_run, "demo_keys", lambda: ["demos/a.dem"])
        monkeypatch.setattr(cluster_run, "append_trend", lambda rows: None)
        monkeypatch.setattr(cluster_run, "write_rollup", lambda r, s: None)
        monkeypatch.setattr(cluster_run, "publish_report", lambda r, s: None)
        monkeypatch.setattr(cluster_run, "write_artifacts", lambda m, k, s: None)

        class FakeS3:
            def download_file(self, bucket, key, path):
                pathlib.Path(path).write_bytes(b"demo")

        monkeypatch.setattr(cluster_run, "s3", lambda: FakeS3())
        monkeypatch.setattr(cluster_run.analyze, "analyze", lambda dem: {
            "demo": dem.name, "map": "de_nuke", "rounds": 24, "stack": [], "players": {},
            "tradeable": {"a": {"b": 1}}})

        assert cluster_run.main() == 0
        saved = written[cluster_run.RESULTS_KEY]
        assert len(saved) == 1
        assert "tradeable" in saved[0]      # the fresh parse won, not the stored one

    def test_entries_without_a_source_key_still_have_an_identity(self):
        assert cluster_run.match_key({"demo": "a.dem"}) == "a.dem"
        assert cluster_run.match_key({"demo": "a.dem", "source_key": "demos/a"}) == "demos/a"


class TestConfig:
    def test_window_is_three_weeks_by_default(self):
        assert cluster_run.WINDOW_DAYS == 21

    def test_trend_fields_match_the_row_builder(self, match):
        row = cluster_run.row_for(match, "k")
        assert list(row) == cluster_run.TREND_FIELDS


class TestDemoFetchGates:
    def test_demo_fetching_is_off_by_default(self, monkeypatch):
        """Off unless asked: without Downloads API access it cannot succeed."""
        assert cluster_run.FETCH_DEMOS is False
        logged = []
        monkeypatch.setattr(cluster_run, "log", logged.append)
        assert cluster_run.fetch_faceit({}) == []
        assert any("disabled" in m for m in logged)


class TestDownloadsGate:
    def test_no_downloads_token_skips_the_fetch_entirely(self, monkeypatch):
        """Data API demo URLs are private; without the Downloads token there is nothing
        to try, so the run must not walk the whole window failing per match."""
        monkeypatch.setattr(cluster_run, "FETCH_DEMOS", True)
        monkeypatch.setattr(cluster_run, "API_KEY", "key")
        monkeypatch.setattr(cluster_run, "DOWNLOADS_TOKEN", "")
        called = []
        monkeypatch.setattr(cluster_run, "faceit_history", lambda: called.append(1) or [])
        logged = []
        monkeypatch.setattr(cluster_run, "log", logged.append)
        assert cluster_run.fetch_faceit({}) == []
        assert not called
        assert any("FACEIT_DOWNLOADS_TOKEN" in m for m in logged)


class TestStatsInterval:
    def test_stats_are_skipped_when_synced_recently(self, monkeypatch):
        """The job runs every 15 minutes; the API window only changes when a match ends."""
        import datetime as dt
        monkeypatch.setattr(cluster_run, "FETCH_STATS", True)
        monkeypatch.setattr(cluster_run, "API_KEY", "key")
        monkeypatch.setattr(cluster_run, "get_json", lambda key, default: default)
        called = []
        monkeypatch.setattr(cluster_run.faceit_stats, "collect",
                            lambda *a, **k: called.append(1) or [])
        logged = []
        monkeypatch.setattr(cluster_run, "log", logged.append)
        now = int(dt.datetime.now(dt.timezone.utc).timestamp())
        cluster_run.sync_stats({"last_stats_sync": now - 600})   # 10 minutes ago
        assert not called
        assert any("skipping" in m for m in logged)

    def test_stats_run_when_the_interval_has_passed(self, monkeypatch):
        import datetime as dt
        monkeypatch.setattr(cluster_run, "FETCH_STATS", True)
        monkeypatch.setattr(cluster_run, "API_KEY", "key")
        monkeypatch.setattr(cluster_run, "get_json", lambda key, default: default)
        monkeypatch.setattr(cluster_run, "put_json", lambda key, obj: None)
        called = []
        monkeypatch.setattr(cluster_run.faceit_stats, "collect",
                            lambda *a, **k: called.append(1) or [])
        monkeypatch.setattr(cluster_run, "log", lambda m: None)
        state = {"last_stats_sync": int(dt.datetime.now(dt.timezone.utc).timestamp()) - 7200}
        cluster_run.sync_stats(state)
        assert called
        assert state["last_stats_sync"] > 0
