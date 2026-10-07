from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.db import DbSession
from app.models import Project
from app.schemas import DaySummaryOut, TestTrendOut
from app.trends import DaySummary, TestTrend, daily_summary, failing_tests, slowest_tests

router = APIRouter(prefix="/projects/{project_id}/trends", tags=["trends"])

# How many of the most recently finished runs to look at. Each test's numbers come with
# the same number of runs before those, for comparison.
Window = Annotated[int, Query(ge=1, le=500)]
Limit = Annotated[int, Query(ge=1, le=100)]


def _check_project(db: Session, project_id: int) -> None:
    if db.get(Project, project_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "project not found")


@router.get("/slowest-tests", response_model=list[TestTrendOut])
def get_slowest_tests(
    project_id: int, db: DbSession, runs: Window = 50, limit: Limit = 10
) -> list[TestTrend]:
    """The tests with the highest median duration over the last `runs` runs."""
    _check_project(db, project_id)
    return slowest_tests(db, project_id, runs, limit)


@router.get("/failing-tests", response_model=list[TestTrendOut])
def get_failing_tests(
    project_id: int, db: DbSession, runs: Window = 50, limit: Limit = 10
) -> list[TestTrend]:
    """The tests with the highest failure rate over the last `runs` runs."""
    _check_project(db, project_id)
    return failing_tests(db, project_id, runs, limit)


@router.get("/daily", response_model=list[DaySummaryOut])
def get_daily_summary(
    project_id: int, db: DbSession, days: Annotated[int, Query(ge=1, le=365)] = 30
) -> list[DaySummary]:
    """Runs, pass rate and median run time for each of the last `days` UTC days."""
    _check_project(db, project_id)
    return daily_summary(db, project_id, days)
