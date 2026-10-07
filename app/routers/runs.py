from fastapi import APIRouter, HTTPException, status
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import DbSession
from app.flaky import flaky_test_names
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


@router.post("/{run_id}/rerun", response_model=RunOut, status_code=status.HTTP_202_ACCEPTED)
def rerun(run_id: int, db: DbSession) -> Run:
    """Queue the same commit again, e.g. to find out whether a failure is flaky."""
    run = _get_or_404(db, run_id)
    if run.commit_sha is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "this run has no commit to test again: it hasn't finished, or it never got as far "
            "as checking one out. Trigger a new run for its ref instead.",
        )
    # Every rerun points to the commit's first run, so they can be counted together.
    first_run_id = run.rerun_of_id or run.id
    again = Run(project_id=run.project_id, ref=run.commit_sha, rerun_of_id=first_run_id)
    db.add(again)
    db.commit()
    db.refresh(again)
    return again


@router.get("/{run_id}/tests", response_model=list[TestResultOut])
def list_test_results(
    run_id: int, db: DbSession, outcome: Outcome | None = None
) -> list[TestResultOut]:
    """Every test in the run, in report order. Filter with e.g. `?outcome=failed`."""
    run = _get_or_404(db, run_id)
    query = select(TestResult).where(TestResult.run_id == run_id)
    if outcome is not None:
        query = query.where(TestResult.outcome == outcome)
    flaky = flaky_test_names(db, run.project_id)
    return [
        TestResultOut.model_validate(result).model_copy(
            update={"flaky": (result.classname, result.name) in flaky}
        )
        for result in db.scalars(query.order_by(TestResult.id))
    ]


@router.get("/{run_id}/log", response_class=PlainTextResponse)
def get_log(run_id: int, db: DbSession) -> str:
    """The commands the run executed and everything they printed."""
    return _get_or_404(db, run_id).log
