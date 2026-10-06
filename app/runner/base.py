import enum
from dataclasses import dataclass
from typing import Protocol


class Outcome(enum.StrEnum):
    """One test's result, using JUnit's four outcomes."""

    PASSED = "passed"
    FAILED = "failed"  # an assertion didn't hold
    ERROR = "error"  # the test crashed before it could pass or fail, e.g. in its setup
    SKIPPED = "skipped"


@dataclass(frozen=True)
class RunSpec:
    """Everything a runner needs to test one version of a repository."""

    repo_url: str
    ref: str  # the branch, tag or commit to test
    test_command: str
    report_path: str  # where the test command writes JUnit XML, relative to the repository
    setup_command: str = ""
    timeout_seconds: int = 600  # for the setup and test commands together


@dataclass(frozen=True)
class CaseResult:
    """One test case, as read from the report."""

    classname: str  # e.g. "tests.test_api"
    name: str  # e.g. "test_create_project"
    outcome: Outcome
    duration_seconds: float
    message: str | None = None  # why it failed, errored or was skipped


@dataclass(frozen=True)
class RunResult:
    """What happened in a run, as plain facts. Deciding pass or fail is the worker's job."""

    commit_sha: str | None = None
    exit_code: int | None = None  # of the test command
    cases: tuple[CaseResult, ...] = ()
    # Set when the tests couldn't run or report at all: a bad ref, a failed setup, a timeout.
    error: str | None = None
    log: str = ""


class Runner(Protocol):
    """Anything that can execute a RunSpec: on this machine today, in a container later."""

    def run(self, spec: RunSpec) -> RunResult:
        """Check out the code, run the tests, and report what happened.

        Failing tests, failed setups and timeouts are results, returned in RunResult.
        Raising is only for bugs in the runner itself.
        """
        ...
