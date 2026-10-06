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
from contextlib import contextmanager, suppress
from datetime import UTC, datetime

from sqlalchemy import update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings, get_settings
from app.db import Base, SessionLocal, engine
from app.models import Project, Run, RunStatus, TestResult
from app.run_queue import Claim, claim_next_run, held, recover_abandoned_runs, send_heartbeat
from app.runner import LocalRunner, Outcome, Runner, RunResult, RunSpec

logger = logging.getLogger(__name__)


def decide_status(result: RunResult) -> RunStatus:
    """Passed only if the test command succeeded and the report shows nothing broken.

    Both have to agree: a report can be all green while the command fails on something
    else (a coverage threshold, a crash after the last test), and the reverse.
    """
    if result.error is not None:
        return RunStatus.ERROR
    broken = any(case.outcome in (Outcome.FAILED, Outcome.ERROR) for case in result.cases)
    return RunStatus.PASSED if result.exit_code == 0 and not broken else RunStatus.FAILED


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
        _save(session_factory, claim, stopped, decide_status(stopped))
        raise
    status = decide_status(result)
    if not _save(session_factory, claim, result, status):
        logger.warning(
            "run #%d: another worker has taken this run over; discarding this result",
            claim.run_id,
        )
        return False
    logger.info("run #%d: %s", claim.run_id, status)
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
    session_factory: sessionmaker[Session], claim: Claim, result: RunResult, status: RunStatus
) -> bool:
    """Record the outcome, but only if the run is still this claim's to finish."""
    counts = Counter(case.outcome for case in result.cases)
    with session_factory() as db:
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
            )
            for case in result.cases
        )
        db.commit()  # the status and every test result land together, or not at all
    return True


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
    Base.metadata.create_all(engine)
    runner = LocalRunner(settings.workspace_dir, settings.max_log_bytes)
    with suppress(KeyboardInterrupt):
        run_worker(SessionLocal, runner, settings, once=args.once)


if __name__ == "__main__":
    main()
