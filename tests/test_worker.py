import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.config import Settings
from app.flaky import find_flaky_tests
from app.models import QuarantinedTest, Run, RunStatus, TestResult
from app.run_queue import claim_next_run, recover_abandoned_runs
from app.runner import CaseResult, Outcome, RunResult, RunSpec
from app.worker import decide_status, execute_run, run_worker

PASSED = CaseResult("tests.test_a", "test_ok", Outcome.PASSED, 0.1)
FAILED = CaseResult("tests.test_a", "test_bad", Outcome.FAILED, 0.2, "assert 1 == 2")
ERRORED = CaseResult("tests.test_b", "test_crash", Outcome.ERROR, 0.0, "boom")
SKIPPED = CaseResult("tests.test_b", "test_later", Outcome.SKIPPED, 0.0, "not ready")

COMMIT = "c" * 40
PASSING = RunResult(commit_sha=COMMIT, exit_code=0, cases=(PASSED,))
FAILING = RunResult(commit_sha=COMMIT, exit_code=1, cases=(PASSED, FAILED))


class FakeRunner:
    """Returns canned results in order, repeating the last one, or raises. Remembers what
    it was asked to run.

    `while_running` is called in the middle of each run, to act out whatever else
    happens in the meantime.
    """

    def __init__(
        self,
        *results: RunResult,
        raises: BaseException | None = None,
        while_running=None,
    ) -> None:
        self.results = list(results) or [PASSING]
        self.raises = raises
        self.while_running = while_running
        self.specs: list[RunSpec] = []

    def run(self, spec: RunSpec) -> RunResult:
        self.specs.append(spec)
        if self.while_running is not None:
            self.while_running()
        if self.raises is not None:
            raise self.raises
        return self.results.pop(0) if len(self.results) > 1 else self.results[0]


def _get(session_factory, run_id) -> Run:
    with session_factory() as db:
        return db.get_one(Run, run_id)


def _all_runs(session_factory) -> list[Run]:
    with session_factory() as db:
        return list(db.scalars(select(Run).order_by(Run.id)))


def _claim(session_factory, worker_id="w1"):
    with session_factory() as db:
        return claim_next_run(db, worker_id)


def _execute(session_factory, claim, runner, heartbeat_seconds=60.0) -> bool:
    return execute_run(session_factory, claim, runner, heartbeat_seconds=heartbeat_seconds)


def _work_through_the_queue(session_factory, runner) -> None:
    run_worker(session_factory, runner, Settings(worker_poll_seconds=0.01), once=True)


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


REAL_BUG = CaseResult("tests.test_c", "test_real_bug", Outcome.FAILED, 0.1, "assert False")
QUARANTINED = {("tests.test_a", "test_bad"), ("tests.test_b", "test_crash")}


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        # Every failure is in a quarantined test: passed, although the tool exited 1.
        (RunResult(exit_code=1, cases=(PASSED, FAILED)), RunStatus.PASSED),
        (RunResult(exit_code=1, cases=(PASSED, ERRORED)), RunStatus.PASSED),
        # One failure outside quarantine is enough to fail the run.
        (RunResult(exit_code=1, cases=(FAILED, REAL_BUG)), RunStatus.FAILED),
        # Nothing failed, so quarantine can't be why the command exited non-zero.
        (RunResult(exit_code=1, cases=(PASSED,)), RunStatus.FAILED),
        (RunResult(error="timed out"), RunStatus.ERROR),
    ],
)
def test_decide_status_with_quarantined_tests(result, expected):
    assert decide_status(result, QUARANTINED) == expected


def _quarantine(session_factory, project, case: CaseResult) -> None:
    # Straight into the table: the evidence check is the API's job, tested there.
    with session_factory() as db:
        db.add(
            QuarantinedTest(
                project_id=project.id, classname=case.classname, name=case.name, reason="flaky"
            )
        )
        db.commit()


def test_failures_in_quarantined_tests_do_not_fail_the_run(
    session_factory, make_project, queue_run
):
    project = make_project()
    _quarantine(session_factory, project, FAILED)
    run_id = queue_run(project)

    _execute(session_factory, _claim(session_factory), FakeRunner(FAILING))

    run = _get(session_factory, run_id)
    assert (run.status, run.tests_failed, run.tests_quarantined) == (RunStatus.PASSED, 1, 1)
    with session_factory() as db:
        flags = {result.name: result.quarantined for result in db.scalars(select(TestResult))}
    assert flags == {"test_ok": False, "test_bad": True}  # the failure is still on record
    assert len(_all_runs(session_factory)) == 1  # it passed, so there's nothing to rerun


def test_a_real_failure_still_fails_a_run_with_quarantined_ones(
    session_factory, make_project, queue_run
):
    project = make_project()
    _quarantine(session_factory, project, FAILED)
    run_id = queue_run(project)
    result = RunResult(commit_sha=COMMIT, exit_code=1, cases=(FAILED, REAL_BUG))

    _execute(session_factory, _claim(session_factory), FakeRunner(result))

    run = _get(session_factory, run_id)
    assert (run.status, run.tests_failed, run.tests_quarantined) == (RunStatus.FAILED, 2, 1)
    assert len(_all_runs(session_factory)) == 2  # a real failure, so its commit is rerun


def test_the_quarantine_at_the_time_the_run_is_saved_decides(
    session_factory, make_project, queue_run
):
    project = make_project()
    run_id = queue_run(project)

    def quarantine_meanwhile() -> None:
        _quarantine(session_factory, project, FAILED)

    _execute(
        session_factory,
        _claim(session_factory),
        FakeRunner(FAILING, while_running=quarantine_meanwhile),
    )

    assert _get(session_factory, run_id).status == RunStatus.PASSED


def test_execute_run_saves_the_outcome_and_every_test(session_factory, make_project, queue_run):
    project = make_project(setup_command="pip install -r requirements.txt", timeout_seconds=120)
    run_id = queue_run(project, ref="feature/x")
    cases = (PASSED, FAILED, ERRORED, SKIPPED)
    runner = FakeRunner(RunResult(commit_sha=COMMIT, exit_code=1, cases=cases, log="the log"))

    assert _execute(session_factory, _claim(session_factory), runner) is True

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
        run = db.get_one(Run, run_id)
        assert run.status == RunStatus.FAILED
        assert run.commit_sha == COMMIT
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

    _execute(session_factory, _claim(session_factory), FakeRunner(raises=RuntimeError("a bug")))

    run = _get(session_factory, run_id)
    assert run.status == RunStatus.ERROR
    assert run.error == "internal error in the runner: a bug"


def test_stopping_the_worker_mid_run_is_recorded(session_factory, make_project, queue_run):
    run_id = queue_run(make_project())
    runner = FakeRunner(raises=KeyboardInterrupt())

    with pytest.raises(KeyboardInterrupt):
        _execute(session_factory, _claim(session_factory), runner)

    run = _get(session_factory, run_id)
    assert run.status == RunStatus.ERROR
    assert run.error == "the worker was stopped during this run"


def test_a_result_from_a_replaced_worker_is_thrown_away(session_factory, make_project, queue_run):
    run_id = queue_run(make_project())
    claim = _claim(session_factory, "w1")

    def replaced_meanwhile() -> None:
        # w1 stalls (say, the laptop sleeps) long enough to be presumed dead, and w2
        # takes the run over. Then w1 wakes up and finishes.
        with session_factory() as db:
            db.get_one(Run, run_id).heartbeat_at = datetime.now(UTC) - timedelta(minutes=5)
            db.commit()
            recover_abandoned_runs(db, lease_seconds=60, max_attempts=3)
            claim_next_run(db, "w2")

    runner = FakeRunner(FAILING, while_running=replaced_meanwhile)

    assert _execute(session_factory, claim, runner) is False

    run = _get(session_factory, run_id)
    assert (run.status, run.worker_id, run.attempt) == (RunStatus.RUNNING, "w2", 2)
    with session_factory() as db:
        assert db.scalars(select(TestResult).where(TestResult.run_id == run_id)).all() == []
    assert len(_all_runs(session_factory)) == 1  # and its failure queued no rerun


def _long_run(session_factory, checks: list[int]):
    """2.5 s of "work", during which another worker looks for abandoned runs every 0.25 s,
    with a 1 s lease."""

    def work() -> None:
        for _ in range(10):
            time.sleep(0.25)
            with session_factory() as db:
                checks.append(recover_abandoned_runs(db, lease_seconds=1, max_attempts=3))

    return work


def test_heartbeats_keep_a_long_run_from_looking_abandoned(
    session_factory, make_project, queue_run
):
    run_id = queue_run(make_project())
    checks: list[int] = []
    runner = FakeRunner(while_running=_long_run(session_factory, checks))

    saved = _execute(session_factory, _claim(session_factory), runner, heartbeat_seconds=0.1)

    assert checks == [0] * 10  # never presumed dead, though the run outlasted the lease
    assert saved is True
    assert _get(session_factory, run_id).status == RunStatus.PASSED
    assert not [t for t in threading.enumerate() if t.name.startswith("heartbeat-")]


def test_without_heartbeats_a_long_run_is_presumed_dead(session_factory, make_project, queue_run):
    # The same run with heartbeats too slow to matter: what the test above guards against.
    run_id = queue_run(make_project())
    checks: list[int] = []
    runner = FakeRunner(while_running=_long_run(session_factory, checks))

    saved = _execute(session_factory, _claim(session_factory), runner, heartbeat_seconds=60)

    assert sum(checks) == 1  # recovered once the lease ran out
    assert saved is False
    assert _get(session_factory, run_id).status == RunStatus.QUEUED  # waiting for attempt 2


def test_a_failed_run_queues_a_rerun_of_its_commit(session_factory, make_project, queue_run):
    run_id = queue_run(make_project(), ref="main")

    _execute(session_factory, _claim(session_factory), FakeRunner(FAILING))

    first, rerun = _all_runs(session_factory)
    assert first.id == run_id
    # The exact commit that failed, not the branch, which may have moved on since.
    assert (rerun.status, rerun.ref, rerun.rerun_of_id) == (RunStatus.QUEUED, COMMIT, run_id)


@pytest.mark.parametrize(
    "result",
    [PASSING, RunResult(commit_sha=COMMIT, error="the setup command exited with code 1")],
    ids=["passed", "error"],
)
def test_only_failed_runs_are_rerun(session_factory, make_project, queue_run, result):
    queue_run(make_project())

    _execute(session_factory, _claim(session_factory), FakeRunner(result))

    assert len(_all_runs(session_factory)) == 1


def test_auto_reruns_can_be_turned_off(session_factory, make_project, queue_run):
    queue_run(make_project(auto_reruns=0))

    _execute(session_factory, _claim(session_factory), FakeRunner(FAILING))

    assert len(_all_runs(session_factory)) == 1


def test_reruns_stop_at_the_projects_limit(session_factory, make_project, queue_run):
    queue_run(make_project(auto_reruns=2), ref="main")
    runner = FakeRunner(FAILING)  # fails every time

    _work_through_the_queue(session_factory, runner)

    first, *reruns = _all_runs(session_factory)
    assert [run.status for run in (first, *reruns)] == [RunStatus.FAILED] * 3
    assert [run.rerun_of_id for run in reruns] == [first.id, first.id]
    assert [spec.ref for spec in runner.specs] == ["main", COMMIT, COMMIT]


def test_a_test_that_fails_and_then_passes_its_rerun_is_flaky(
    session_factory, make_project, queue_run
):
    project = make_project()
    queue_run(project)
    passes_this_time = CaseResult("tests.test_a", "test_bad", Outcome.PASSED, 0.2)
    rerun_result = RunResult(commit_sha=COMMIT, exit_code=0, cases=(PASSED, passes_this_time))

    _work_through_the_queue(session_factory, FakeRunner(FAILING, rerun_result))

    first, rerun = _all_runs(session_factory)
    assert (first.status, rerun.status) == (RunStatus.FAILED, RunStatus.PASSED)
    with session_factory() as db:
        (flaky,) = find_flaky_tests(db, project.id)
    assert flaky.name == "test_bad"
    assert (flaky.latest.failed_run_id, flaky.latest.passed_run_id) == (first.id, rerun.id)


def test_worker_recovers_abandoned_runs_before_claiming(session_factory, make_project, queue_run):
    run_id = queue_run(make_project())
    _claim(session_factory, "dead-worker")
    with session_factory() as db:
        db.get_one(Run, run_id).heartbeat_at = datetime.now(UTC) - timedelta(minutes=5)
        db.commit()

    _work_through_the_queue(session_factory, FakeRunner())

    run = _get(session_factory, run_id)
    assert (run.status, run.attempt) == (RunStatus.PASSED, 2)
    assert run.worker_id != "dead-worker"


def test_worker_runs_everything_queued_then_exits_when_told_once(
    session_factory, make_project, queue_run
):
    project = make_project()
    run_ids = [queue_run(project) for _ in range(3)]
    runner = FakeRunner()

    _work_through_the_queue(session_factory, runner)

    assert len(runner.specs) == 3
    assert [_get(session_factory, run_id).status for run_id in run_ids] == [RunStatus.PASSED] * 3
