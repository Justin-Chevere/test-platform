from datetime import date, datetime
from pathlib import PurePosixPath, PureWindowsPath
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator

from app.models import RunStatus
from app.runner import Outcome

# A branch, tag or commit. It can't start with "-", so git can never read it as an option.
Ref = Annotated[str, Field(min_length=1, max_length=255, pattern=r"^[\w.][\w./-]*$")]


class ProjectCreate(BaseModel):
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")
    # HTTPS only: no SSH keys to manage, and no file:// paths into this machine.
    repo_url: str = Field(max_length=255, pattern=r"^https://\S+$")
    default_branch: Ref = "main"
    # Runs first, in the same virtualenv, e.g. "python -m pip install -r requirements.txt".
    setup_command: str = Field(default="", max_length=1000)
    # Must write a JUnit XML report to report_path.
    test_command: str = Field(
        default="python -m pytest --junitxml=test-report.xml", min_length=1, max_length=1000
    )
    report_path: str = Field(default="test-report.xml", min_length=1, max_length=255)
    # For the setup and test commands together.
    timeout_seconds: int = Field(default=600, ge=10, le=3600)
    # When a run fails, test the same commit again up to this many times, to tell flaky
    # tests from broken ones. 0 turns it off.
    auto_reruns: int = Field(default=1, ge=0, le=3)

    @field_validator("report_path")
    @classmethod
    def _inside_the_repository(cls, value: str) -> str:
        # Checked as both kinds of path, since workers may run on either kind of system.
        for path in (PurePosixPath(value), PureWindowsPath(value)):
            if path.anchor or ".." in path.parts:
                raise ValueError("must be a relative path inside the repository")
        return value


class ProjectOut(BaseModel):
    # from_attributes lets Pydantic read straight from the SQLAlchemy object.
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    repo_url: str
    default_branch: str
    setup_command: str
    test_command: str
    report_path: str
    timeout_seconds: int
    auto_reruns: int
    created_at: datetime


class RunCreate(BaseModel):
    ref: Ref | None = None  # unset: the project's default branch


class RunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int
    ref: str
    commit_sha: str | None
    rerun_of_id: int | None  # set when this run tests the same commit as an earlier one
    status: RunStatus
    worker_id: str | None
    attempt: int
    heartbeat_at: datetime | None
    exit_code: int | None
    error: str | None
    tests_passed: int
    tests_failed: int
    tests_errored: int
    tests_skipped: int
    tests_quarantined: int  # failed or errored, but quarantined, so they didn't fail the run
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None

    @computed_field
    @property
    def duration_seconds(self) -> float | None:
        if self.started_at is None or self.finished_at is None:
            return None
        return round((self.finished_at - self.started_at).total_seconds(), 1)


class TestResultOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    classname: str
    name: str
    outcome: Outcome
    duration_seconds: float
    message: str | None
    # Quarantined when this run was saved: if it failed here, that didn't fail the run.
    quarantined: bool
    # It has both passed and failed on one commit in this project, so a failure here may
    # be the test's fault rather than the code's.
    flaky: bool = False


class FlakyEvidenceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    commit_sha: str
    failed_run_id: int
    passed_run_id: int


class FlakyTestOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    classname: str
    name: str
    flaky_commits: int  # how many commits it has both passed and failed on
    last_flaked_at: datetime
    latest: FlakyEvidenceOut  # from the most recent of those commits


class QuarantineCreate(BaseModel):
    classname: str = Field(max_length=1000)  # can be empty: some tools don't report one
    name: str = Field(min_length=1, max_length=1000)
    reason: str = Field(min_length=1, max_length=500)  # why, and who is fixing it


class QuarantinedTestOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    classname: str
    name: str
    reason: str
    created_at: datetime
    runs_since: int  # runs since it went into quarantine that included the test...
    failures_since: int  # ...and how many of those it failed or errored in


class TrendWindowOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    runs: int  # runs that ran the test
    failures: int  # ...in which it failed or errored
    failure_rate: float
    median_seconds: float  # how long it typically takes
    p95_seconds: float  # how long it takes on a bad day: 1 run in 20 is slower


class TestTrendOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    classname: str
    name: str
    recent: TrendWindowOut  # the last N runs
    previous: TrendWindowOut | None  # the N runs before those; None if it didn't run then


class DaySummaryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    day: date  # a UTC day
    runs: int
    passed: int
    failed: int
    errors: int
    pass_rate: float | None  # None on a day without runs
    median_duration_seconds: float | None
