from datetime import UTC, datetime, timedelta

import pytest

from app.models import Run, RunStatus, TestResult
from app.runner import Outcome

FLAKY = {"classname": "tests.test_api", "name": "test_timing"}
BROKEN = {"classname": "tests.test_api", "name": "test_bug"}
REASON = "races the cache warm-up; Justin is on it"


@pytest.fixture
def record(session_factory):
    """Save a finished run with an outcome for each test, `minutes` from now."""

    def _record(project_id: int, commit: str, outcomes: dict[str, Outcome], minutes=0) -> int:
        with session_factory() as db:
            run = Run(
                project_id=project_id,
                ref=commit,
                commit_sha=commit,
                status=RunStatus.PASSED,
                attempt=1,
                finished_at=datetime.now(UTC) + timedelta(minutes=minutes),
            )
            db.add(run)
            db.flush()
            db.add_all(
                TestResult(
                    run_id=run.id,
                    classname="tests.test_api",
                    name=name,
                    outcome=outcome,
                    duration_seconds=0.1,
                )
                for name, outcome in outcomes.items()
            )
            db.commit()
            return run.id

    return _record


@pytest.fixture
def project_id(make_project, record) -> int:
    """A project where test_timing is flaky and test_bug fails every time, a day ago."""
    project = make_project()
    day_ago = -24 * 60
    failing = {"test_timing": Outcome.FAILED, "test_bug": Outcome.FAILED}
    record(project.id, "aaa", failing, minutes=day_ago)
    record(project.id, "aaa", failing | {"test_timing": Outcome.PASSED}, minutes=day_ago + 1)
    return project.id


def _quarantine(client, project_id, test=FLAKY, reason=REASON):
    return client.post(f"/projects/{project_id}/quarantine", json=test | {"reason": reason})


def test_quarantine_a_flaky_test(client, project_id):
    response = _quarantine(client, project_id)

    assert response.status_code == 201
    entry = response.json()
    assert (entry["classname"], entry["name"], entry["reason"], entry["created_by"]) == (
        "tests.test_api",
        "test_timing",
        REASON,
        "admin",  # who the client is logged in as
    )
    assert (entry["runs_since"], entry["failures_since"]) == (0, 0)
    assert client.get(f"/projects/{project_id}/quarantine").json() == [entry]


def test_a_test_that_fails_every_time_cannot_be_quarantined(client, project_id):
    response = _quarantine(client, project_id, test=BROKEN)

    assert response.status_code == 409
    assert response.json()["detail"].startswith("no evidence this test is flaky")


def test_a_test_can_only_be_quarantined_once(client, project_id):
    _quarantine(client, project_id)

    assert _quarantine(client, project_id).status_code == 409


def test_quarantining_needs_a_reason(client, project_id):
    assert _quarantine(client, project_id, reason="").status_code == 422


def test_quarantine_of_a_missing_project(client):
    assert client.get("/projects/999/quarantine").status_code == 404
    assert _quarantine(client, 999).status_code == 404


def test_the_list_shows_how_each_test_has_done_since_it_went_in(client, project_id, record):
    _quarantine(client, project_id)
    # After quarantine: failed once, passed twice. The two runs from before don't count.
    record(project_id, "bbb", {"test_timing": Outcome.FAILED}, minutes=1)
    record(project_id, "bbb", {"test_timing": Outcome.PASSED}, minutes=2)
    record(project_id, "ccc", {"test_timing": Outcome.PASSED, "test_bug": Outcome.FAILED}, 3)
    record(project_id, "ccc", {"test_bug": Outcome.FAILED}, minutes=4)  # without test_timing

    (entry,) = client.get(f"/projects/{project_id}/quarantine").json()

    assert (entry["runs_since"], entry["failures_since"]) == (3, 1)


def test_release_a_test_from_quarantine(client, project_id, make_project):
    entry = _quarantine(client, project_id).json()
    other = make_project("other")

    assert client.delete(f"/projects/{other.id}/quarantine/{entry['id']}").status_code == 404
    assert client.delete(f"/projects/{project_id}/quarantine/{entry['id']}").status_code == 204
    assert client.get(f"/projects/{project_id}/quarantine").json() == []
    assert client.delete(f"/projects/{project_id}/quarantine/{entry['id']}").status_code == 404
