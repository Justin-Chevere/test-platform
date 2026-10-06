from fastapi import APIRouter, HTTPException, status
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import DbSession
from app.models import Run, TestResult
from app.runner import Outcome
from app.schemas import RunOut, TestResultOut

router = APIRouter(prefix="/runs", tags=["runs"])


def _get_or_404(db: Session, run_id: int) -> Run:
    run = db.get(Run, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found")
    return run


@router.get("/{run_id}", response_model=RunOut)
def get_run(run_id: int, db: DbSession) -> Run:
    return _get_or_404(db, run_id)


@router.get("/{run_id}/tests", response_model=list[TestResultOut])
def list_test_results(
    run_id: int, db: DbSession, outcome: Outcome | None = None
) -> list[TestResult]:
    """Every test in the run, in report order. Filter with e.g. `?outcome=failed`."""
    _get_or_404(db, run_id)
    query = select(TestResult).where(TestResult.run_id == run_id)
    if outcome is not None:
        query = query.where(TestResult.outcome == outcome)
    return list(db.scalars(query.order_by(TestResult.id)))


@router.get("/{run_id}/log", response_class=PlainTextResponse)
def get_log(run_id: int, db: DbSession) -> str:
    """The commands the run executed and everything they printed."""
    return _get_or_404(db, run_id).log
