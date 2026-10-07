"""Flaky tests: tests that both passed and failed on the same commit.

Same code, different result, so it's the test that's unreliable (timing, randomness, test
order, state left behind by an earlier run), not the code it tests. Failing on one commit
and passing on the next doesn't count: that's a fix. The evidence comes from testing a
commit more than once, which reruns do, whether asked for or automatic after a failure.
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.models import Run, TestResult
from app.runner import Outcome


@dataclass(frozen=True)
class Evidence:
    commit_sha: str
    failed_run_id: int  # a run of that commit in which the test failed or errored...
    passed_run_id: int  # ...and one in which it passed


@dataclass(frozen=True)
class FlakyTest:
    classname: str
    name: str
    flaky_commits: int  # how many commits it has both passed and failed on
    last_flaked_at: datetime
    latest: Evidence  # from the most recent of those commits


def find_flaky_tests(db: Session, project_id: int) -> list[FlakyTest]:
    """The project's flaky tests, the most recently flaky first."""
    passed = TestResult.outcome == Outcome.PASSED
    failed = TestResult.outcome.in_([Outcome.FAILED, Outcome.ERROR])  # skipped is neither
    # One row per test and commit, kept only if the test both passed and failed there.
    # The database does the filtering; Python only shapes the few rows that are left.
    rows = db.execute(
        select(
            TestResult.classname,
            TestResult.name,
            Run.commit_sha,
            func.max(case((failed, Run.id))).label("failed_run_id"),
            func.max(case((passed, Run.id))).label("passed_run_id"),
            func.max(Run.finished_at).label("finished_at"),
        )
        .join(Run, Run.id == TestResult.run_id)
        .where(Run.project_id == project_id, Run.commit_sha.is_not(None))
        .group_by(TestResult.classname, TestResult.name, Run.commit_sha)
        .having(func.count(case((passed, 1))) > 0, func.count(case((failed, 1))) > 0)
    ).all()

    by_test = defaultdict(list)
    for row in rows:
        by_test[row.classname, row.name].append(row)
    flaky = []
    for (classname, name), commits in by_test.items():
        latest = max(commits, key=lambda row: row.finished_at)
        flaky.append(
            FlakyTest(
                classname=classname,
                name=name,
                flaky_commits=len(commits),
                last_flaked_at=latest.finished_at,
                latest=Evidence(latest.commit_sha, latest.failed_run_id, latest.passed_run_id),
            )
        )
    return sorted(flaky, key=lambda test: test.last_flaked_at, reverse=True)


def flaky_test_names(db: Session, project_id: int) -> set[tuple[str, str]]:
    """The (classname, name) of every test that has been flaky in the project."""
    return {(test.classname, test.name) for test in find_flaky_tests(db, project_id)}
