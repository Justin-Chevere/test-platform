"""The run queue lives in the database: a queued run is just a row with status "queued".

That's one less service to operate (no Redis or RabbitMQ), and a run can never be saved
but not queued, because saving it is queuing it. Rails 8's default job queue, Solid
Queue, keeps its jobs in the database the same way.
"""

from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session, aliased

from app.models import Run, RunStatus


def claim_next_run(db: Session, worker_id: str) -> Run | None:
    """Mark the oldest queued run as running for this worker, and return it.

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
    run_id = db.scalar(
        update(Run)
        .where(Run.id == oldest, Run.status == RunStatus.QUEUED)
        .values(status=RunStatus.RUNNING, worker_id=worker_id, started_at=datetime.now(UTC))
        .returning(Run.id)
    )
    db.commit()
    return db.get(Run, run_id) if run_id is not None else None
