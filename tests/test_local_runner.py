"""The local runner end to end: real git repositories, virtualenvs and processes."""

import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from app.runner import LocalRunner, Outcome, RunSpec

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

REPORT = """<testsuites><testsuite name="demo">
  <testcase classname="demo" name="test_ok" time="0.01"/>
  <testcase classname="demo" name="test_bad"><failure message="assert 1 == 2"/></testcase>
  <testcase classname="demo" name="test_later"><skipped message="not ready"/></testcase>
</testsuite></testsuites>"""

# Stands in for a test tool: prints which Python is running it, writes a report, and
# exits with the code it's given.
FAKE_TESTS = f"""import sys
from pathlib import Path

print("python:", sys.prefix)
Path("test-report.xml").write_text({REPORT!r})
sys.exit(int(sys.argv[1]))
"""


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "--all")
    author = ["-c", "user.name=Test", "-c", "user.email=test@example.com"]
    _git(repo, *author, "commit", "--quiet", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "origin"
    path.mkdir()
    _git(path, "init", "--quiet", "--initial-branch=main")
    return path


@pytest.fixture
def runner(tmp_path: Path) -> LocalRunner:
    workspaces = tmp_path / "workspaces"
    workspaces.mkdir()
    return LocalRunner(workspace_root=workspaces)


def _spec(repo: Path, **overrides: Any) -> RunSpec:
    fields = {
        "repo_url": str(repo),
        "ref": "main",
        "test_command": "python fake_tests.py 1",
        "report_path": "test-report.xml",
        "timeout_seconds": 60,
    }
    return RunSpec(**(fields | overrides))


def test_runs_the_tests_in_a_fresh_virtualenv_and_reads_the_report(repo, runner):
    (repo / "fake_tests.py").write_text(FAKE_TESTS)
    sha = _commit(repo, "Add tests")

    result = runner.run(_spec(repo))

    assert result.error is None
    assert result.commit_sha == sha
    assert result.exit_code == 1
    assert [(c.name, c.outcome) for c in result.cases] == [
        ("test_ok", Outcome.PASSED),
        ("test_bad", Outcome.FAILED),
        ("test_later", Outcome.SKIPPED),
    ]
    assert "$ git fetch" in result.log
    assert "$ python fake_tests.py 1" in result.log
    # The tests ran on the run's own virtualenv, not on the Python running the worker.
    python_line = next(line for line in result.log.splitlines() if line.startswith("python:"))
    assert Path(python_line.removeprefix("python:").strip()).name == "venv"
    # And nothing was left behind.
    assert list(runner.workspace_root.iterdir()) == []


def test_checks_out_the_exact_commit_asked_for(repo, runner):
    (repo / "fake_tests.py").write_text(FAKE_TESTS)
    first = _commit(repo, "Add tests")
    (repo / "fake_tests.py").unlink()
    _commit(repo, "Remove tests")

    result = runner.run(_spec(repo, ref=first, test_command="python fake_tests.py 0"))

    assert result.error is None
    assert result.commit_sha == first
    assert result.exit_code == 0


def test_an_unknown_ref_is_an_error(repo, runner):
    (repo / "fake_tests.py").write_text(FAKE_TESTS)
    _commit(repo, "Add tests")

    result = runner.run(_spec(repo, ref="no-such-branch"))

    assert result.error == f"could not check out 'no-such-branch' from {repo}"
    assert result.commit_sha is None


def test_a_failed_setup_stops_the_run(repo, runner):
    (repo / "fake_tests.py").write_text(FAKE_TESTS)
    _commit(repo, "Add tests")

    result = runner.run(_spec(repo, setup_command='python -c "import sys; sys.exit(3)"'))

    assert result.error == "the setup command exited with code 3"
    assert result.cases == ()
    assert "$ python fake_tests.py" not in result.log  # the tests never started


def test_a_committed_report_does_not_count_as_this_runs_report(repo, runner):
    (repo / "test-report.xml").write_text(REPORT)
    _commit(repo, "Commit a stale report by mistake")

    result = runner.run(_spec(repo, test_command='python -c "print(1)"'))

    assert result.error == (
        "the test command exited with code 0 and wrote no report at test-report.xml"
    )
    assert result.cases == ()


def test_a_timeout_kills_the_command_and_everything_it_started(repo, runner, tmp_path):
    started_marker = tmp_path / "started.txt"
    survivor_marker = tmp_path / "survivor.txt"
    # The test command starts a child process that would write a file 4 seconds later,
    # then sleeps far past the time limit.
    child = f"import pathlib, time; time.sleep(4); pathlib.Path({str(survivor_marker)!r}).touch()"
    (repo / "spawner.py").write_text(
        "import pathlib, subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, '-c', {child!r}])\n"
        f"pathlib.Path({str(started_marker)!r}).touch()\n"
        "time.sleep(60)\n"
    )
    _commit(repo, "Add a test that hangs")

    began = time.monotonic()
    result = runner.run(_spec(repo, test_command="python spawner.py", timeout_seconds=3))

    assert result.error == "the setup and test commands took over 3 s"
    assert time.monotonic() - began < 30  # nowhere near the 60 s the command wanted
    assert started_marker.exists()  # the child really was started...
    time.sleep(4)
    assert not survivor_marker.exists()  # ...and was killed along with its parent
