"""The image itself: what ships in it, and what it pins.

These exist because report.py was added to the repo, imported by the entrypoint, and
left out of the Dockerfile's COPY - which only surfaced as a ModuleNotFoundError in the
CronJob, at 04:00, after a full ARM build and a push.
"""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")
SHIPPED = {"analyze.py", "team.py", "report.py", "cluster_run.py"}


def copied_modules():
    copied = set()
    for line in DOCKERFILE.splitlines():
        if line.startswith("COPY "):
            copied |= {tok for tok in line.split()[1:] if tok.endswith(".py")}
    return copied


def test_every_module_in_the_repo_is_copied_into_the_image():
    """A module in the repo that the image lacks fails at runtime, not at build."""
    in_repo = {p.name for p in ROOT.glob("*.py")} - {"conftest.py"}
    missing = in_repo - copied_modules()
    assert not missing, f"not COPYed into the image: {sorted(missing)}"


def test_copy_does_not_reference_files_that_no_longer_exist():
    for name in copied_modules():
        assert (ROOT / name).exists(), f"Dockerfile copies missing file {name}"


def test_entrypoint_imports_are_all_shipped():
    """Whatever cluster_run imports from this repo has to be in the image."""
    src = (ROOT / "cluster_run.py").read_text(encoding="utf-8")
    local = {f"{m}.py" for m in re.findall(r"^import (\w+)$", src, re.M)} & SHIPPED
    assert local <= copied_modules()


def test_build_runs_an_import_check():
    """The build must fail on a missing module rather than the pod."""
    assert "import analyze, report, team, cluster_run" in DOCKERFILE


def test_version_is_pinned_and_parseable():
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    assert re.fullmatch(r"\d+\.\d+\.\d+", version), version


def test_requirements_are_bounded():
    """An unpinned dependency turns a rebuild into a lottery on ARM."""
    for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            assert re.search(r"[=<>]", line), f"unbounded requirement: {line}"


def test_image_runs_as_a_non_root_user():
    assert re.search(r"^USER \w+", DOCKERFILE, re.M), "no USER line: container would run as root"
