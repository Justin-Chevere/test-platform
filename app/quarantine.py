"""Quarantine: a flaky test whose failures don't fail runs while someone fixes it.

Quarantining is a deliberate decision with a reason, never automatic: a test that flaked
once and was ignored from then on could hide a real bug for good. It's also only for
tests with evidence of being flaky (see app/flaky.py). A test that fails every time is
broken, and quarantine must not become a way to mute it.

The list shows how each test has done since it went in, so tests get released once fixed
rather than staying in quarantine by default.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import and_, case, delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.flaky import flaky_test_names
from app.models import QuarantinedTest, Run, TestResult
from app.runner import Outcome


class NotFlaky(Exception):
    """The test has never both passed and failed on one commit."""


class AlreadyQuarantined(Exception):
    pass


@dataclass(frozen=True)
class QuarantineEntry:
    id: int
    classname: str
    name: str
    reason: str
    created_by: str | None
    created_at: datetime
    runs_since: int  # runs since it went into quarantine that included the test...
    failures_since: int  # ...and how many of those it failed or errored in


def quarantine_test(
    db: Session, project_id: int, classname: str, name: str, reason: str, created_by: str
) -> int:
    """Quarantine a flaky test, and return the entry's id."""
    if (classname, name) not in flaky_test_names(db, project_id):
        raise NotFlaky
    entry = QuarantinedTest(
        project_id=project_id,
        classname=classname,
        name=name,
        reason=reason,
        created_by=created_by,
    )
    db.add(entry)
    try:
        db.commit()
    except IntegrityError as exc:  # the unique constraint: it's already in
        db.rollback()
        raise AlreadyQuarantined from exc
    return entry.id


def release_test(db: Session, project_id: int, entry_id: int) -> bool:
    """Take a test out of quarantine. False if the project has no such entry."""
    released = db.execute(
        delete(QuarantinedTest).where(
            QuarantinedTest.id == entry_id, QuarantinedTest.project_id == project_id
        )
    )
    db.commit()
    return released.rowcount == 1


def quarantined_test_names(db: Session, project_id: int) -> set[tuple[str, str]]:
    """The (classname, name) of every test in the project's quarantine."""
    rows = db.execute(
        select(QuarantinedTest.classname, QuarantinedTest.name).where(
            QuarantinedTest.project_id == project_id
        )
    )
    return {(row.classname, row.name) for row in rows}


def list_quarantine(db: Session, project_id: int) -> list[QuarantineEntry]:
    """The project's quarantined tests, oldest first, each with how it has done since."""
    failed = TestResult.outcome.in_([Outcome.FAILED, Outcome.ERROR])
    rows = db.execute(
        select(
            QuarantinedTest,
            func.count(TestResult.id).label("runs_since"),
            func.count(case((failed, 1))).label("failures_since"),
        )
        # Every run finished since the test went in, and the test's result in it, if any.
        .outerjoin(
            Run,
            and_(
                Run.project_id == QuarantinedTest.project_id,
                Run.finished_at >= QuarantinedTest.created_at,
            ),
        )
        .outerjoin(
            TestResult,
            and_(
                TestResult.run_id == Run.id,
                TestResult.classname == QuarantinedTest.classname,
                TestResult.name == QuarantinedTest.name,
            ),
        )
        .where(QuarantinedTest.project_id == project_id)
        .group_by(QuarantinedTest.id)
        .order_by(QuarantinedTest.created_at, QuarantinedTest.id)
    )
    return [
        QuarantineEntry(
            id=entry.id,
            classname=entry.classname,
            name=entry.name,
            reason=entry.reason,
            created_by=entry.created_by,
            created_at=entry.created_at,
            runs_since=runs_since,
            failures_since=failures_since,
        )
        for entry, runs_since, failures_since in rows
    ]
