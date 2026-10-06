import pytest
from sqlalchemy import select

from app.models import Run, RunStatus, TestResult
from app.run_queue import claim_next_run
from app.runner import CaseResult, Outcome, RunResult, RunSpec
from app.worker import decide_status, execute_run, run_worker

PASSED = CaseResult("tests.test_a", "test_ok", Outcome.PASSED, 0.1)
FAILED = CaseResult("tests.test_a", "test_bad", Outcome.FAILED, 0.2, "assert 1 == 2")
ERRORED = CaseResult("tests.test_b", "test_crash", Outcome.ERROR, 0.0, "boom")
SKIPPED = CaseResult("tests.test_b", "test_later", Outcome.SKIPPED, 0.0, "not ready")


class FakeRunner:
    """Returns a canned result, or raises, and remembers what it was asked to run."""

    def __init__(
        self, result: RunResult | None = None, raises: BaseException | None = None
    ) -> None:
        self.result = result or RunResult(commit_sha="c" * 40, exit_code=0, cases=(PASSED,))
        self.raises = raises
        self.specs: list[RunSpec] = []

    def run(self, spec: RunSpec) -> RunResult:
        self.specs.append(spec)
        if self.raises is not None:
            raise self.raises
        return self.result


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (RunResult(exit_code=0, cases=(PASSED, SKIPPED)), RunStatus.PASSED),
        (RunResult(exit_code=1, cases=(PASSED, FAILED)), RunStatus.FAILED),
        (RunResult(exit_code=1, cases=(PASSED, ERRORED)), RunStatus.FAILED),
        # The report and the exit code have to agree; either alone can mislead.
        (RunResult(exit_code=0, cases=(FAILED,)), RunStatus.FAILED),
        (RunResult(exit_code=2, cases=(PASSED,)), RunStatus.FAILED),
        (RunResult(error="timed out"), RunStatus.ERROR),
    ],
)
def test_decide_status(result, expected):
    assert decide_status(result) == expected


def test_execute_run_saves_the_outcome_and_every_test(session_factory, make_project, queue_run):
    project = make_project(setup_command="pip install -r requirements.txt", timeout_seconds=120)
    run_id = queue_run(project, ref="feature/x")
    cases = (PASSED, FAILED, ERRORED, SKIPPED)
    runner = FakeRunner(RunResult(commit_sha="c" * 40, exit_code=1, cases=cases, log="the log"))

    with session_factory() as db:
        execute_run(db, claim_next_run(db, "w1"), runner)

    assert runner.specs == [
        RunSpec(
            repo_url=project.repo_url,
            ref="feature/x",
            setup_command="pip install -r requirements.txt",
            test_command=project.test_command,
            report_path=project.report_path,
            timeout_seconds=120,
        )
    ]
    with session_factory() as db:
        run = db.get(Run, run_id)
        assert run.status == RunStatus.FAILED
        assert run.commit_sha == "c" * 40
        assert run.exit_code == 1
        assert (run.tests_passed, run.tests_failed, run.tests_errored, run.tests_skipped) == (
            1,
            1,
            1,
            1,
        )
        assert run.log == "the log"
        assert run.finished_at >= run.started_at
        saved = db.scalars(select(TestResult).where(TestResult.run_id == run_id)).all()
        assert [(r.name, r.outcome, r.message) for r in saved] == [
            (case.name, case.outcome, case.message) for case in cases
        ]


def test_a_crashing_runner_marks_the_run_as_error(session_factory, make_project, queue_run):
    run_id = queue_run(make_project())

    with session_factory() as db:
        execute_run(db, claim_next_run(db, "w1"), FakeRunner(raises=RuntimeError("a bug")))

    with session_factory() as db:
        run = db.get(Run, run_id)
        assert run.status == RunStatus.ERROR
        assert run.error == "internal error in the runner: a bug"


def test_stopping_the_worker_mid_run_is_recorded(session_factory, make_project, queue_run):
    run_id = queue_run(make_project())

    with session_factory() as db, pytest.raises(KeyboardInterrupt):
        execute_run(db, claim_next_run(db, "w1"), FakeRunner(raises=KeyboardInterrupt()))

    with session_factory() as db:
        run = db.get(Run, run_id)
        assert run.status == RunStatus.ERROR
        assert run.error == "the worker was stopped during this run"


def test_worker_runs_everything_queued_then_exits_when_told_once(
    session_factory, make_project, queue_run
):
    project = make_project()
    run_ids = [queue_run(project) for _ in range(3)]
    runner = FakeRunner()

    run_worker(session_factory, runner, poll_seconds=0.01, once=True)

    assert len(runner.specs) == 3
    with session_factory() as db:
        assert [db.get(Run, run_id).status for run_id in run_ids] == [RunStatus.PASSED] * 3
