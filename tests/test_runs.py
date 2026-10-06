from datetime import UTC, datetime, timedelta

import pytest

from app.models import Run, RunStatus, TestResult
from app.runner import Outcome


@pytest.fixture
def finished_run(session_factory, make_project) -> int:
    """A failed run with three test results, as a worker would have saved it."""
    project = make_project()
    started = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
    with session_factory() as db:
        run = Run(
            project_id=project.id,
            ref="main",
            commit_sha="a" * 40,
            status=RunStatus.FAILED,
            exit_code=1,
            tests_passed=1,
            tests_failed=1,
            tests_skipped=1,
            log="$ python -m pytest\n1 failed, 1 passed, 1 skipped\n",
            started_at=started,
            finished_at=started + timedelta(seconds=42.27),
        )
        db.add(run)
        db.flush()  # assigns run.id
        for name, outcome, message in [
            ("test_ok", Outcome.PASSED, None),
            ("test_bad", Outcome.FAILED, "assert 1 == 2"),
            ("test_later", Outcome.SKIPPED, "not ready"),
        ]:
            db.add(
                TestResult(
                    run_id=run.id,
                    classname="tests.test_a",
                    name=name,
                    outcome=outcome,
                    duration_seconds=0.2,
                    message=message,
                )
            )
        db.commit()
        return run.id


def test_get_run(client, finished_run):
    response = client.get(f"/runs/{finished_run}")

    assert response.status_code == 200
    run = response.json()
    assert run["status"] == "failed"
    assert run["commit_sha"] == "a" * 40
    assert (run["tests_passed"], run["tests_failed"], run["tests_skipped"]) == (1, 1, 1)
    assert run["duration_seconds"] == 42.3  # rounded to a tenth of a second
    assert "log" not in run  # too big for every response: it has its own endpoint


def test_list_test_results_in_report_order(client, finished_run):
    tests = client.get(f"/runs/{finished_run}/tests").json()

    assert [t["name"] for t in tests] == ["test_ok", "test_bad", "test_later"]


def test_filter_test_results_by_outcome(client, finished_run):
    tests = client.get(f"/runs/{finished_run}/tests?outcome=failed").json()

    assert tests == [
        {
            "classname": "tests.test_a",
            "name": "test_bad",
            "outcome": "failed",
            "duration_seconds": 0.2,
            "message": "assert 1 == 2",
        }
    ]


def test_get_log_as_plain_text(client, finished_run):
    response = client.get(f"/runs/{finished_run}/log")

    assert response.headers["content-type"].startswith("text/plain")
    assert "1 failed, 1 passed, 1 skipped" in response.text


@pytest.mark.parametrize("path", ["/runs/999", "/runs/999/tests", "/runs/999/log"])
def test_missing_run(client, path):
    assert client.get(path).status_code == 404
