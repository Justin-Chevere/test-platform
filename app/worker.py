"""A worker takes queued runs one at a time and executes them.

Start as many as you like, on one machine or on several sharing a database: each run goes
to exactly one of them (see run_queue.claim_next_run). Workers are separate from the API
on purpose: test runs are long and heavy, so the two scale and restart independently.

While it executes a run, a worker sends heartbeats. If it dies, the heartbeats stop, and
another worker puts the run back in the queue (see run_queue.recover_abandoned_runs).

    python -m app.worker           # wait for runs until stopped with Ctrl+C
    python -m app.worker --once    # run everything queued, then exit
"""

import argparse
import logging
import os
import socket
import threading
import time
from collections import Counter
from collections.abc import Iterator
from collections.abc import Set as AbstractSet
from contextlib import contextmanager, suppress
from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings, get_settings
from app.db import SessionLocal, engine
from app.migrate import SchemaOutOfDate, check_schema
from app.models import Project, Run, RunStatus, TestResult
from app.quarantine import quarantined_test_names
from app.run_queue import Claim, claim_next_run, held, recover_abandoned_runs, send_heartbeat
from app.runner import LocalRunner, Outcome, Runner, RunResult, RunSpec

logger = logging.getLogger(__name__)


def decide_status(
    result: RunResult, quarantined: AbstractSet[tuple[str, str]] = frozenset()
) -> RunStatus:
    """Passed if the test command succeeded and the report shows nothing broken, or if
    everything broken is a quarantined test.

    Normally both have to agree: a report can be all green while the command fails on
    something else (a coverage threshold, a crash after the last test), and the reverse.
    Quarantine is the exception: the test tool exits non-zero precisely because the
    quarantined tests failed, so then the report decides on its own.
    """
    if result.error is not None:
        return RunStatus.ERROR
    broken = [case for case in result.cases if case.outcome in (Outcome.FAILED, Outcome.ERROR)]
    if not broken:
        return RunStatus.PASSED if result.exit_code == 0 else RunStatus.FAILED
    if all((case.classname, case.name) in quarantined for case in broken):
        return RunStatus.PASSED
    return RunStatus.FAILED


def execute_run(
    session_factory: sessionmaker[Session],
    claim: Claim,
    runner: Runner,
    *,
    heartbeat_seconds: float,
) -> bool:
    """Execute a claimed run and save its outcome.

    Returns False if the run was handed to another worker in the meantime (this one was
    presumed dead), in which case the outcome is thrown away.
    """
    # Short database sessions before and after the run, none during it: a run can take
    # many minutes, and a transaction shouldn't stay open that long.
    with session_factory() as db:
        run = db.get_one(Run, claim.run_id)
        project = db.get_one(Project, run.project_id)
        project_id = project.id
        spec = RunSpec(
            repo_url=project.repo_url,
            ref=run.ref,
            setup_command=project.setup_command,
            test_command=project.test_command,
            report_path=project.report_path,
            timeout_seconds=project.timeout_seconds,
        )
    logger.info("run #%d: testing %s (attempt %d)", claim.run_id, spec.ref, claim.attempt)
    try:
        with _heartbeats(session_factory, claim, heartbeat_seconds):
            result = runner.run(spec)
    except Exception as exc:
        # A bug in the runner fails the run now, with the reason, rather than leaving it
        # to look abandoned and be retried.
        logger.exception("run #%d: the runner crashed", claim.run_id)
        result = RunResult(error=f"internal error in the runner: {exc}")
    except BaseException:
        # Ctrl+C or a shutdown in the middle of a run: record it before stopping.
        stopped = RunResult(error="the worker was stopped during this run")
        _save(session_factory, claim, stopped, project_id)
        raise
    if not _save(session_factory, claim, result, project_id):
        logger.warning(
            "run #%d: another worker has taken this run over; discarding this result",
            claim.run_id,
        )
        return False
    return True


@contextmanager
def _heartbeats(
    session_factory: sessionmaker[Session], claim: Claim, interval: float
) -> Iterator[None]:
    """Renew the claim's lease every `interval` seconds while the with-block runs.

    The beats come from a background thread, because the worker itself is busy waiting
    for the tests. The thread has its own database sessions, as sessions can't be shared
    between threads.
    """
    stop = threading.Event()

    def beat() -> None:
        # wait() returns True as soon as stop is set, so the loop ends without delay.
        while not stop.wait(interval):
            try:
                with session_factory() as db:
                    still_ours = send_heartbeat(db, claim)
            except SQLAlchemyError:
                # One missed heartbeat isn't fatal: the lease allows for several.
                logger.warning("run #%d: heartbeat failed", claim.run_id, exc_info=True)
                continue
            if not still_ours:
                logger.warning("run #%d: lost the run to another worker", claim.run_id)
                return

    # daemon: if the worker dies, this thread never keeps the process alive on its own.
    thread = threading.Thread(target=beat, name=f"heartbeat-run-{claim.run_id}", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join()


def _save(
    session_factory: sessionmaker[Session], claim: Claim, result: RunResult, project_id: int
) -> bool:
    """Record the outcome, but only if the run is still this claim's to finish."""
    counts = Counter(case.outcome for case in result.cases)
    with session_factory() as db:
        # Read in the same transaction that records the verdict: the quarantine as it is
        # now decides, even if it changed while the tests were running.
        quarantined = quarantined_test_names(db, project_id)
        status = decide_status(result, quarantined)
        let_through = sum(
            1
            for case in result.cases
            if case.outcome in (Outcome.FAILED, Outcome.ERROR)
            and (case.classname, case.name) in quarantined
        )
        # Fenced: matches nothing if the run was handed to another worker meanwhile.
        updated = db.execute(
            update(Run)
            .where(held(claim))
            .values(
                status=status,
                commit_sha=result.commit_sha,
                exit_code=result.exit_code,
                error=result.error,
                log=result.log,
                tests_passed=counts[Outcome.PASSED],
                tests_failed=counts[Outcome.FAILED],
                tests_errored=counts[Outcome.ERROR],
                tests_skipped=counts[Outcome.SKIPPED],
                tests_quarantined=let_through,
                finished_at=datetime.now(UTC),
            )
        )
        if updated.rowcount == 0:
            db.rollback()
            return False
        db.add_all(
            TestResult(
                run_id=claim.run_id,
                classname=case.classname,
                name=case.name,
                outcome=case.outcome,
                duration_seconds=case.duration_seconds,
                message=case.message,
                quarantined=(case.classname, case.name) in quarantined,
            )
            for case in result.cases
        )
        rerun_id = None
        if status == RunStatus.FAILED and result.commit_sha is not None:
            rerun_id = _queue_auto_rerun(db, claim.run_id, result.commit_sha)
        # The status, every test result and any rerun land together, or not at all.
        db.commit()
    if let_through:
        logger.info(
            "run #%d: %s, not counting %d failure(s) in quarantined tests",
            claim.run_id,
            status,
            let_through,
        )
    else:
        logger.info("run #%d: %s", claim.run_id, status)
    if rerun_id is not None:
        logger.info(
            "run #%d: queued run #%d to test commit %s again",
            claim.run_id,
            rerun_id,
            result.commit_sha[:12],
        )
    return True


def _queue_auto_rerun(db: Session, run_id: int, commit_sha: str) -> int | None:
    """Queue a failed run's commit again, if its project allows another rerun.

    Tests that fail and then pass on the same commit are flaky (see app/flaky.py).
    Returns the new run's id, or None if the project has no reruns left for it.
    """
    run = db.get_one(Run, run_id)
    project = db.get_one(Project, run.project_id)
    first_run_id = run.rerun_of_id or run.id
    reruns_so_far = db.scalar(
        select(func.count()).select_from(Run).where(Run.rerun_of_id == first_run_id)
    )
    if reruns_so_far >= project.auto_reruns:
        return None
    rerun = Run(project_id=project.id, ref=commit_sha, rerun_of_id=first_run_id)
    db.add(rerun)
    db.flush()  # assigns its id
    return rerun.id


def run_worker(
    session_factory: sessionmaker[Session],
    runner: Runner,
    settings: Settings,
    *,
    once: bool = False,
) -> None:
    # Host and process id: enough to tell which worker ran what, on one machine or many.
    worker_id = f"{socket.gethostname()}-{os.getpid()}"
    logger.info("worker %s is waiting for runs", worker_id)
    while True:
        with session_factory() as db:
            recovered = recover_abandoned_runs(
                db, lease_seconds=settings.lease_seconds, max_attempts=settings.max_attempts
            )
            claim = claim_next_run(db, worker_id)
        if recovered:
            logger.warning("recovered %d run(s) whose worker stopped responding", recovered)
        if claim is not None:
            execute_run(
                session_factory, claim, runner, heartbeat_seconds=settings.heartbeat_seconds
            )
            continue
        if once:
            return
        time.sleep(settings.worker_poll_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(description="Execute queued test runs.")
    parser.add_argument(
        "--once", action="store_true", help="exit when the queue is empty instead of waiting"
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = get_settings()
    try:
        check_schema(engine)
    except SchemaOutOfDate as exc:
        parser.exit(1, f"error: {exc}\n")
    runner = LocalRunner(settings.workspace_dir, settings.max_log_bytes)
    with suppress(KeyboardInterrupt):
        run_worker(SessionLocal, runner, settings, once=args.once)


if __name__ == "__main__":
    main()
