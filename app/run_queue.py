"""The run queue lives in the database: a queued run is just a row with status "queued".

That's one less service to operate (no Redis or RabbitMQ), and a run can never be saved
but not queued, because saving it is queuing it. Rails 8's default job queue, Solid
Queue, keeps its jobs in the database the same way.

A claim is a lease, not ownership forever: the worker has to keep sending heartbeats,
or its run is presumed abandoned and handed to another worker.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import ColumnElement, and_, select, update
from sqlalchemy.orm import Session, aliased

from app.models import Run, RunStatus


@dataclass(frozen=True)
class Claim:
    """A worker's hold on a run.

    `attempt` says which claim of the run this is: 1 for its first worker, 2 if that one
    died, and so on. It doubles as a fencing token: a worker may only write to the run
    while the run is still on its attempt, so a worker that was presumed dead and comes
    back can never overwrite the work of the worker that replaced it.
    """

    run_id: int
    attempt: int


def held(claim: Claim) -> ColumnElement[bool]:
    """SQL condition: the run is still running under this claim."""
    return and_(
        Run.id == claim.run_id,
        Run.status == RunStatus.RUNNING,
        Run.attempt == claim.attempt,
    )


def claim_next_run(db: Session, worker_id: str) -> Claim | None:
    """Mark the oldest queued run as running for this worker, and return the claim.

    One UPDATE both picks the run and claims it, and only matches a run that's still
    queued. The database applies writes one at a time, so two workers can never claim
    the same run: whichever comes second finds it already taken.
    """
    queued = aliased(Run)  # a separate name, so the inner query isn't tied to the outer row
    oldest = (
        select(queued.id)
        .where(queued.status == RunStatus.QUEUED)
        .order_by(queued.id)
        .limit(1)
        .scalar_subquery()
    )
    now = datetime.now(UTC)
    row = db.execute(
        update(Run)
        .where(Run.id == oldest, Run.status == RunStatus.QUEUED)
        .values(
            status=RunStatus.RUNNING,
            worker_id=worker_id,
            attempt=Run.attempt + 1,
            started_at=now,
            heartbeat_at=now,  # the lease starts now
        )
        .returning(Run.id, Run.attempt)
    ).one_or_none()
    db.commit()
    return Claim(run_id=row.id, attempt=row.attempt) if row is not None else None


def send_heartbeat(db: Session, claim: Claim) -> bool:
    """Renew the claim's lease. False if the run was taken away from this worker."""
    renewed = db.execute(update(Run).where(held(claim)).values(heartbeat_at=datetime.now(UTC)))
    db.commit()
    return renewed.rowcount == 1


def recover_abandoned_runs(db: Session, *, lease_seconds: float, max_attempts: int) -> int:
    """Put runs whose worker stopped sending heartbeats back in the queue.

    Every worker calls this before claiming, so recovery needs no process of its own:
    as long as one worker is alive, no run stays stuck. A run already claimed
    `max_attempts` times is failed instead, so a test that kills its worker every time
    can't go around forever. Returns how many runs were recovered.
    """
    now = datetime.now(UTC)
    abandoned = and_(
        Run.status == RunStatus.RUNNING,
        Run.heartbeat_at < now - timedelta(seconds=lease_seconds),
    )
    gave_up = db.execute(
        update(Run)
        .where(abandoned, Run.attempt >= max_attempts)
        .values(
            status=RunStatus.ERROR,
            error=f"gave up after {max_attempts} attempts: every worker that ran it "
            "stopped responding",
            finished_at=now,
        )
    )
    requeued = db.execute(
        update(Run)
        .where(abandoned, Run.attempt < max_attempts)
        .values(status=RunStatus.QUEUED, worker_id=None, started_at=None, heartbeat_at=None)
    )
    db.commit()
    return gave_up.rowcount + requeued.rowcount
