from dataclasses import astuple
from datetime import UTC, date, datetime, timedelta

import pytest

from app.models import Run, RunStatus, TestResult
from app.runner import Outcome
from app.trends import daily_summary, failing_tests, slowest_tests, trends_by_test

P, F, E, S = Outcome.PASSED, Outcome.FAILED, Outcome.ERROR, Outcome.SKIPPED
START = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)


def at(minutes: float) -> datetime:
    return START + timedelta(minutes=minutes)


@pytest.fixture
def project(make_project):
    return make_project()


@pytest.fixture
def record(session_factory, project):
    """Save a finished run. `results` maps a test name to its (outcome, seconds)."""

    def _record(
        results: dict[str, tuple[Outcome, float]],
        finished_at: datetime,
        run_seconds: float = 60.0,
        status: RunStatus | None = None,
        project_id: int | None = None,
    ) -> int:
        broken = any(outcome in (F, E) for outcome, _ in results.values())
        with session_factory() as db:
            run = Run(
                project_id=project_id or project.id,
                ref="main",
                commit_sha="a" * 40,
                status=status or (RunStatus.FAILED if broken else RunStatus.PASSED),
                attempt=1,
                started_at=finished_at - timedelta(seconds=run_seconds),
                finished_at=finished_at,
            )
            db.add(run)
            db.flush()
            db.add_all(
                TestResult(
                    run_id=run.id,
                    classname="tests.test_app",
                    name=name,
                    outcome=outcome,
                    duration_seconds=seconds,
                )
                for name, (outcome, seconds) in results.items()
            )
            db.commit()
            return run.id

    return _record


def _trends(session_factory, project, window):
    with session_factory() as db:
        return {trend.name: trend for trend in trends_by_test(db, project.id, window)}


def test_slowest_tests_rank_by_median_and_show_the_bad_days(session_factory, project, record):
    # test_slow takes 1 s, except once 9 s. Its mean would be 1.8 s, a time it never
    # actually took; the median says what's typical and p95 shows the bad run.
    for minute in range(10):
        slow = 9.0 if minute == 3 else 1.0
        record(
            {"test_slow": (P, slow), "test_medium": (P, 0.5), "test_fast": (P, 0.1)}, at(minute)
        )

    with session_factory() as db:
        slowest = slowest_tests(db, project.id, window=10, limit=10)

    assert [test.name for test in slowest] == ["test_slow", "test_medium", "test_fast"]
    assert (slowest[0].recent.median_seconds, slowest[0].recent.p95_seconds) == (1.0, 9.0)
    assert slowest[0].recent.runs == 10
    assert slowest[0].previous is None  # there were no runs before these ten

    with session_factory() as db:
        assert len(slowest_tests(db, project.id, window=10, limit=2)) == 2


def test_each_window_is_compared_with_the_one_before(session_factory, project, record):
    record({"test_cache": (P, 0.1)}, at(1))
    record({"test_cache": (P, 0.1)}, at(2))
    record({"test_cache": (P, 1.0)}, at(3))  # someone made it ten times slower
    record({"test_cache": (P, 1.0)}, at(4))

    trend = _trends(session_factory, project, window=2)["test_cache"]

    assert (trend.recent.median_seconds, trend.recent.runs) == (1.0, 2)
    assert (trend.previous.median_seconds, trend.previous.runs) == (0.1, 2)


def test_recent_means_finished_most_recently(session_factory, project, record):
    # The first run queued is the last to finish: its result is the newest evidence.
    record({"test_x": (P, 2.0)}, finished_at=at(10), run_seconds=600)
    record({"test_x": (P, 1.0)}, finished_at=at(5))

    trend = _trends(session_factory, project, window=1)["test_x"]

    assert (trend.recent.median_seconds, trend.previous.median_seconds) == (2.0, 1.0)


def test_a_skip_is_not_a_run_and_an_error_is_a_failure(session_factory, project, record):
    for minute, outcome in enumerate([F, E, P, S]):
        record({"test_x": (outcome, 0.0 if outcome == S else 2.0)}, at(minute))

    trend = _trends(session_factory, project, window=10)["test_x"]

    assert (trend.recent.runs, trend.recent.failures, trend.recent.failure_rate) == (3, 2, 0.667)
    assert trend.recent.median_seconds == 2.0  # the skip's 0 s isn't a duration


def test_failing_tests_rank_by_failure_rate(session_factory, project, record):
    outcomes = {
        "test_rarely": [F, P, P, P],
        "test_often": [F, F, P, P],
        "test_never": [P, P, P, P],
    }
    for i in range(4):
        record({name: (runs[i], 0.1) for name, runs in outcomes.items()}, at(i))

    with session_factory() as db:
        failing = failing_tests(db, project.id, window=4, limit=10)

    assert [(test.name, test.recent.failure_rate) for test in failing] == [
        ("test_often", 0.5),
        ("test_rarely", 0.25),
    ]


def test_a_test_that_no_longer_runs_drops_out(session_factory, project, record):
    record({"test_deleted": (F, 0.1), "test_x": (P, 0.1)}, at(1))
    record({"test_x": (P, 0.1)}, at(2))

    assert set(_trends(session_factory, project, window=1)) == {"test_x"}


def test_only_this_projects_runs_count(session_factory, project, make_project, record):
    other = make_project("other")
    record({"test_x": (F, 5.0)}, at(1), project_id=other.id)

    assert _trends(session_factory, project, window=10) == {}


def test_daily_summary_has_every_utc_day_even_empty_ones(session_factory, project, record):
    record({"test_x": (P, 0.1)}, datetime(2026, 9, 30, 12, 0, tzinfo=UTC))  # before the range
    record({"test_x": (P, 0.1)}, datetime(2026, 10, 1, 8, 0, tzinfo=UTC), run_seconds=60)
    record({"test_x": (F, 0.1)}, datetime(2026, 10, 1, 23, 59, tzinfo=UTC), run_seconds=120)
    record({}, datetime(2026, 10, 3, 0, 1, tzinfo=UTC), 10, status=RunStatus.ERROR)
    record({"test_x": (P, 0.1)}, datetime(2026, 10, 3, 9, 0, tzinfo=UTC), run_seconds=30)

    with session_factory() as db:
        days = daily_summary(db, project.id, days=3, today=date(2026, 10, 3))

    # (day, runs, passed, failed, errors, pass rate, median run seconds)
    assert [astuple(day) for day in days] == [
        (date(2026, 10, 1), 2, 1, 1, 0, 0.5, 90.0),  # 23:59 UTC is still October 1st
        (date(2026, 10, 2), 0, 0, 0, 0, None, None),
        (date(2026, 10, 3), 2, 1, 0, 1, 0.5, 20.0),
    ]


def test_trend_endpoints(client, project, record):
    now = datetime.now(UTC)
    record({"test_slow": (P, 3.0), "test_flaky": (F, 0.1)}, now - timedelta(minutes=2))
    record({"test_slow": (P, 3.0), "test_flaky": (P, 0.1)}, now - timedelta(minutes=1))

    slowest = client.get(f"/projects/{project.id}/trends/slowest-tests?runs=1").json()
    failing = client.get(f"/projects/{project.id}/trends/failing-tests?runs=2").json()
    daily = client.get(f"/projects/{project.id}/trends/daily?days=2").json()

    assert slowest[0] == {
        "classname": "tests.test_app",
        "name": "test_slow",
        "recent": {
            "runs": 1,
            "failures": 0,
            "failure_rate": 0.0,
            "median_seconds": 3.0,
            "p95_seconds": 3.0,
        },
        "previous": {
            "runs": 1,
            "failures": 0,
            "failure_rate": 0.0,
            "median_seconds": 3.0,
            "p95_seconds": 3.0,
        },
    }
    assert [(test["name"], test["recent"]["failure_rate"]) for test in failing] == [
        ("test_flaky", 0.5)
    ]
    # Yesterday and today, between them holding both runs, even if midnight fell between.
    assert len(daily) == 2
    assert sum(day["runs"] for day in daily) == 2


@pytest.mark.parametrize(
    "path", ["slowest-tests?runs=0", "failing-tests?limit=0", "daily?days=366"]
)
def test_trend_parameters_are_bounded(client, project, path):
    assert client.get(f"/projects/{project.id}/trends/{path}").status_code == 422


def test_trends_of_a_missing_project(client):
    assert client.get("/projects/999/trends/daily").status_code == 404
