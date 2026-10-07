from datetime import UTC, datetime, timedelta

import pytest

from app.flaky import Evidence, FlakyTest, find_flaky_tests
from app.models import Project, Run, RunStatus, TestResult
from app.runner import Outcome

PASSED, FAILED, ERROR, SKIPPED = Outcome.PASSED, Outcome.FAILED, Outcome.ERROR, Outcome.SKIPPED
NOON = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)


@pytest.fixture
def project(make_project) -> Project:
    return make_project()


@pytest.fixture
def record(session_factory, project):
    """Save a finished run of `commit`, with an outcome for each named test."""

    def _record(
        commit: str, outcomes: dict[str, Outcome], minute: int = 0, project_id: int | None = None
    ) -> int:
        broken = {FAILED, ERROR} & set(outcomes.values())
        with session_factory() as db:
            run = Run(
                project_id=project_id or project.id,
                ref=commit,
                commit_sha=commit,
                status=RunStatus.FAILED if broken else RunStatus.PASSED,
                attempt=1,
                finished_at=NOON + timedelta(minutes=minute),
            )
            db.add(run)
            db.flush()
            db.add_all(
                TestResult(
                    run_id=run.id,
                    classname="tests.test_app",
                    name=name,
                    outcome=outcome,
                    duration_seconds=0.1,
                )
                for name, outcome in outcomes.items()
            )
            db.commit()
            return run.id

    return _record


def _flaky(session_factory, project_id) -> list[FlakyTest]:
    with session_factory() as db:
        return find_flaky_tests(db, project_id)


def test_a_test_that_passed_and_failed_on_one_commit_is_flaky(session_factory, project, record):
    failed_run = record("aaa", {"test_timing": FAILED, "test_steady": PASSED})
    passed_run = record("aaa", {"test_timing": PASSED, "test_steady": PASSED}, minute=1)

    assert _flaky(session_factory, project.id) == [
        FlakyTest(
            classname="tests.test_app",
            name="test_timing",
            flaky_commits=1,
            last_flaked_at=NOON + timedelta(minutes=1),
            latest=Evidence(commit_sha="aaa", failed_run_id=failed_run, passed_run_id=passed_run),
        )
    ]


def test_failing_on_one_commit_and_passing_on_the_next_is_a_fix(session_factory, project, record):
    record("aaa", {"test_bug": FAILED})
    record("bbb", {"test_bug": PASSED}, minute=1)

    assert _flaky(session_factory, project.id) == []


def test_an_error_counts_as_a_failure_and_a_skip_as_neither(session_factory, project, record):
    record("aaa", {"test_crash": ERROR, "test_optional": SKIPPED})
    record("aaa", {"test_crash": PASSED, "test_optional": PASSED}, minute=1)

    assert [test.name for test in _flaky(session_factory, project.id)] == ["test_crash"]


def test_counts_commits_and_lists_the_most_recently_flaky_first(session_factory, project, record):
    record("aaa", {"test_often": FAILED}, minute=0)
    record("aaa", {"test_often": PASSED}, minute=1)
    record("ccc", {"test_once": PASSED}, minute=5)
    record("ccc", {"test_once": FAILED}, minute=6)
    fail_bbb = record("bbb", {"test_often": FAILED}, minute=10)
    pass_bbb = record("bbb", {"test_often": PASSED}, minute=11)

    often, once = _flaky(session_factory, project.id)

    assert (often.name, often.flaky_commits) == ("test_often", 2)
    assert often.latest == Evidence("bbb", failed_run_id=fail_bbb, passed_run_id=pass_bbb)
    assert (once.name, once.flaky_commits) == ("test_once", 1)


def test_other_projects_runs_are_not_mixed_in(session_factory, make_project, project, record):
    other = make_project("other")
    record("aaa", {"test_timing": FAILED})
    record("aaa", {"test_timing": PASSED}, minute=1, project_id=other.id)

    assert _flaky(session_factory, project.id) == []
    assert _flaky(session_factory, other.id) == []
