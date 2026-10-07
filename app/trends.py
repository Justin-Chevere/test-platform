"""Trends: which tests are slow, which fail most, and which way both are heading.

Per-test numbers cover a window of the project's most recently finished runs, counted in
runs rather than days, so a quiet project and a busy one both get enough samples. Each
comes with the same numbers for the window before, which is what turns a list into a
trend: a test whose median went from 0.1 s to 1 s is a performance regression.

Computed when asked, in Python: medians and percentiles have no portable SQL (Postgres
has percentile_cont, SQLite nothing), and a few hundred runs' worth of results is small.
At a much larger scale, per-test daily totals would be kept up to date as results arrive.
"""

import math
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import Row, select
from sqlalchemy.orm import Session

from app.models import Run, RunStatus, TestResult
from app.runner import Outcome

# Runs that got as far as test results. An errored run never did.
_WITH_RESULTS = (RunStatus.PASSED, RunStatus.FAILED)


@dataclass(frozen=True)
class Window:
    """One test's numbers over a window of runs."""

    runs: int  # runs that ran the test (a skip doesn't count: the test didn't run)
    failures: int  # ...in which it failed or errored
    failure_rate: float
    median_seconds: float  # how long it typically takes
    p95_seconds: float  # how long it takes on a bad day: 1 run in 20 is slower


@dataclass(frozen=True)
class TestTrend:
    __test__ = False  # tells pytest this isn't a test class, despite the name

    classname: str
    name: str
    recent: Window
    previous: Window | None  # the window before; None if the test didn't run then


@dataclass(frozen=True)
class DaySummary:
    day: date  # a UTC day
    runs: int
    passed: int
    failed: int
    errors: int
    pass_rate: float | None  # None on a day without runs
    median_duration_seconds: float | None


def trends_by_test(db: Session, project_id: int, window: int) -> list[TestTrend]:
    """Every test's numbers over the last `window` runs, and the `window` runs before."""
    latest_first = (
        select(Run.id)
        .where(Run.project_id == project_id, Run.status.in_(_WITH_RESULTS))
        # Most recently finished, not most recently queued: a long run started long ago
        # can still be the newest evidence.
        .order_by(Run.finished_at.desc(), Run.id.desc())
        .limit(2 * window)
    )
    run_ids = list(db.scalars(latest_first))
    recent_ids = set(run_ids[:window])
    rows = db.execute(
        select(
            TestResult.run_id,
            TestResult.classname,
            TestResult.name,
            TestResult.outcome,
            TestResult.duration_seconds,
        ).where(TestResult.run_id.in_(latest_first), TestResult.outcome != Outcome.SKIPPED)
    )
    samples: defaultdict[tuple[str, str], tuple[list[Row], list[Row]]] = defaultdict(
        lambda: ([], [])
    )
    for row in rows:
        recent, previous = samples[row.classname, row.name]
        (recent if row.run_id in recent_ids else previous).append(row)
    return [
        TestTrend(classname, name, _window(recent), _window(previous) if previous else None)
        for (classname, name), (recent, previous) in samples.items()
        if recent  # a test that no longer runs has no trend worth showing
    ]


def slowest_tests(db: Session, project_id: int, window: int, limit: int) -> list[TestTrend]:
    trends = trends_by_test(db, project_id, window)
    return sorted(trends, key=lambda trend: trend.recent.median_seconds, reverse=True)[:limit]


def failing_tests(db: Session, project_id: int, window: int, limit: int) -> list[TestTrend]:
    failing = [trend for trend in trends_by_test(db, project_id, window) if trend.recent.failures]
    failing.sort(key=lambda trend: (trend.recent.failure_rate, trend.recent.failures), reverse=True)
    return failing[:limit]


def daily_summary(
    db: Session, project_id: int, days: int, today: date | None = None
) -> list[DaySummary]:
    """One entry per UTC day for the last `days` days, today included, oldest first.

    Days without runs are in the list too, so a chart has no gaps to paper over.
    """
    today = today or datetime.now(UTC).date()
    first_day = today - timedelta(days=days - 1)
    rows = db.execute(
        select(Run.status, Run.started_at, Run.finished_at).where(
            Run.project_id == project_id,
            Run.finished_at >= datetime.combine(first_day, time.min, tzinfo=UTC),
        )
    )
    by_day: defaultdict[date, list[Row]] = defaultdict(list)
    for row in rows:
        by_day[row.finished_at.date()].append(row)
    summary = []
    for offset in range(days):
        day = first_day + timedelta(days=offset)
        runs = by_day.get(day, [])
        statuses = Counter(row.status for row in runs)
        durations = [
            (row.finished_at - row.started_at).total_seconds() for row in runs if row.started_at
        ]
        summary.append(
            DaySummary(
                day=day,
                runs=len(runs),
                passed=statuses[RunStatus.PASSED],
                failed=statuses[RunStatus.FAILED],
                errors=statuses[RunStatus.ERROR],
                pass_rate=round(statuses[RunStatus.PASSED] / len(runs), 3) if runs else None,
                median_duration_seconds=round(statistics.median(durations), 1)
                if durations
                else None,
            )
        )
    return summary


def _window(samples: list[Row]) -> Window:
    durations = sorted(sample.duration_seconds for sample in samples)
    failures = sum(1 for sample in samples if sample.outcome in (Outcome.FAILED, Outcome.ERROR))
    return Window(
        runs=len(samples),
        failures=failures,
        failure_rate=round(failures / len(samples), 3),
        median_seconds=round(statistics.median(durations), 3),
        p95_seconds=round(_percentile(durations, 0.95), 3),
    )


def _percentile(sorted_values: list[float], fraction: float) -> float:
    """Nearest-rank percentile: the smallest value that at least `fraction` of all values
    are less than or equal to. Unlike an interpolated one, it's a duration that really
    happened."""
    return sorted_values[math.ceil(fraction * len(sorted_values)) - 1]
