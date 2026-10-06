"""A worker takes queued runs one at a time and executes them.

Start as many as you like, on one machine or on several sharing a database: each run goes
to exactly one of them (see run_queue.claim_next_run). Workers are separate from the API
on purpose: test runs are long and heavy, so the two scale and restart independently.

    python -m app.worker           # wait for runs until stopped with Ctrl+C
    python -m app.worker --once    # run everything queued, then exit
"""

import argparse
import logging
import os
import socket
import time
from collections import Counter
from contextlib import suppress
from datetime import UTC, datetime

from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.db import Base, SessionLocal, engine
from app.models import Project, Run, RunStatus, TestResult
from app.run_queue import claim_next_run
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


def execute_run(db: Session, run: Run, runner: Runner) -> None:
    project = db.get_one(Project, run.project_id)
    spec = RunSpec(
        repo_url=project.repo_url,
        ref=run.ref,
        setup_command=project.setup_command,
        test_command=project.test_command,
        report_path=project.report_path,
        timeout_seconds=project.timeout_seconds,
    )
    try:
        result = runner.run(spec)
    except Exception as exc:
        # A bug in the runner must not leave the run stuck in "running" forever.
        logger.exception("run #%d: the runner crashed", run.id)
        result = RunResult(error=f"internal error in the runner: {exc}")
    except BaseException:
        # Ctrl+C or a shutdown in the middle of a run: record it before stopping.
        _save(db, run, RunResult(error="the worker was stopped during this run"))
        raise
    _save(db, run, result)


def _save(db: Session, run: Run, result: RunResult) -> None:
    counts = Counter(case.outcome for case in result.cases)
    run.status = decide_status(result)
    run.commit_sha = result.commit_sha
    run.exit_code = result.exit_code
    run.error = result.error
    run.log = result.log
    run.tests_passed = counts[Outcome.PASSED]
    run.tests_failed = counts[Outcome.FAILED]
    run.tests_errored = counts[Outcome.ERROR]
    run.tests_skipped = counts[Outcome.SKIPPED]
    run.finished_at = datetime.now(UTC)
    db.add_all(
        TestResult(
            run_id=run.id,
            classname=case.classname,
            name=case.name,
            outcome=case.outcome,
            duration_seconds=case.duration_seconds,
            message=case.message,
        )
        for case in result.cases
    )
    db.commit()


def run_worker(
    session_factory: sessionmaker[Session],
    runner: Runner,
    *,
    poll_seconds: float,
    once: bool = False,
) -> None:
    # Host and process id: enough to tell which worker ran what, on one machine or many.
    worker_id = f"{socket.gethostname()}-{os.getpid()}"
    logger.info("worker %s is waiting for runs", worker_id)
    while True:
        with session_factory() as db:
            run = claim_next_run(db, worker_id)
            if run is not None:
                logger.info("run #%d: testing %s", run.id, run.ref)
                execute_run(db, run, runner)
                logger.info("run #%d: %s", run.id, run.status)
                continue
        if once:
            return
        time.sleep(poll_seconds)


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
        run_worker(SessionLocal, runner, poll_seconds=settings.worker_poll_seconds, once=args.once)


if __name__ == "__main__":
    main()
