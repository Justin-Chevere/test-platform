import enum
from datetime import UTC, datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Index, String, Text
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from app.db import Base
from app.runner import Outcome


class RunStatus(enum.StrEnum):
    QUEUED = "queued"  # waiting for a worker, or for another one if its worker died
    RUNNING = "running"
    PASSED = "passed"  # the test command succeeded and no test failed
    FAILED = "failed"  # the tests ran, and one failed or the test command exited non-zero
    ERROR = "error"  # the tests couldn't run or report: a bad ref, a failed setup, a timeout


class UTCDateTime(TypeDecorator[datetime]):
    """A datetime column that always comes back timezone-aware UTC, on every database.

    SQLite has no time zones and returns naive datetimes, which the API would send
    without a "Z" and a browser would read as local time. So values are stored as UTC
    and marked UTC on the way back out. Naive values are refused rather than guessed at.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise TypeError("naive datetime: use a timezone-aware one, e.g. datetime.now(UTC)")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        return value.replace(tzinfo=UTC) if value is not None else None


def _enum_column(enum_cls: type[enum.StrEnum]) -> Enum:
    # Store "passed" rather than "PASSED" so the DB values match the API values.
    return Enum(
        enum_cls,
        values_callable=lambda e: [m.value for m in e],
        native_enum=False,
        length=16,
    )


def _now() -> datetime:
    return datetime.now(UTC)


class Project(Base):
    """A repository to test, and how to test it."""

    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(63), unique=True)
    repo_url: Mapped[str] = mapped_column(String(255))
    default_branch: Mapped[str] = mapped_column(String(255))
    setup_command: Mapped[str] = mapped_column(String(1000))
    test_command: Mapped[str] = mapped_column(String(1000))
    report_path: Mapped[str] = mapped_column(String(255))
    timeout_seconds: Mapped[int]
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)


class Run(Base):
    """One execution of a project's tests against one commit."""

    __tablename__ = "runs"
    # Matches the question the queue asks over and over: the oldest queued run.
    __table_args__ = (Index("ix_runs_status_id", "status", "id"),)

    # Integer ids, so runs read like CI build numbers (#42) and sort oldest first.
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    ref: Mapped[str] = mapped_column(String(255))  # what was asked for: a branch, tag or commit
    # What was actually tested. 40 characters for SHA-1, 64 for SHA-256 repositories.
    commit_sha: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[RunStatus] = mapped_column(_enum_column(RunStatus), default=RunStatus.QUEUED)
    worker_id: Mapped[str | None] = mapped_column(String(255))
    # How many times a worker has claimed this run: more than once if a worker died
    # running it. Also the claim's fencing token, see run_queue.Claim.
    attempt: Mapped[int] = mapped_column(default=0)
    # The running worker's last sign of life, see run_queue.recover_abandoned_runs.
    heartbeat_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    exit_code: Mapped[int | None]
    error: Mapped[str | None] = mapped_column(Text)
    tests_passed: Mapped[int] = mapped_column(default=0)
    tests_failed: Mapped[int] = mapped_column(default=0)
    tests_errored: Mapped[int] = mapped_column(default=0)
    tests_skipped: Mapped[int] = mapped_column(default=0)
    # Deferred: only loaded when read, so listing runs never drags their logs along.
    log: Mapped[str] = mapped_column(Text, default="", deferred=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class TestResult(Base):
    """One test case's outcome in one run."""

    __tablename__ = "test_results"
    __test__ = False  # tells pytest this isn't a test class, despite the name

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), index=True)
    # Together these identify a test across runs, e.g. "tests.test_api" + "test_create".
    classname: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    outcome: Mapped[Outcome] = mapped_column(_enum_column(Outcome))
    duration_seconds: Mapped[float]
    message: Mapped[str | None] = mapped_column(Text)
