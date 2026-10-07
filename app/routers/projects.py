from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import AdminUser, DeveloperUser, require_role
from app.db import DbSession
from app.flaky import FlakyTest, find_flaky_tests
from app.models import Project, Role, Run
from app.quarantine import (
    AlreadyQuarantined,
    NotFlaky,
    QuarantineEntry,
    list_quarantine,
    quarantine_test,
    release_test,
)
from app.schemas import (
    FlakyTestOut,
    ProjectCreate,
    ProjectOut,
    QuarantineCreate,
    QuarantinedTestOut,
    RunCreate,
    RunOut,
)

router = APIRouter(
    prefix="/projects",
    tags=["projects"],
    # Deny by default: every route needs at least a signed-in viewer, and the routes
    # that change things raise that further.
    dependencies=[Depends(require_role(Role.VIEWER))],
)


def _get_or_404(db: Session, project_id: int) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "project not found")
    return project


@router.post("", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
def create_project(payload: ProjectCreate, db: DbSession, admin: AdminUser) -> Project:
    """Register a repository. Admins only: its commands are what workers will run."""
    project = Project(**payload.model_dump())
    db.add(project)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "name already in use") from exc
    db.refresh(project)
    return project


@router.get("", response_model=list[ProjectOut])
def list_projects(db: DbSession) -> list[Project]:
    return list(db.scalars(select(Project).order_by(Project.id)))


@router.get("/{project_id}", response_model=ProjectOut)
def get_project(project_id: int, db: DbSession) -> Project:
    return _get_or_404(db, project_id)


@router.post("/{project_id}/runs", response_model=RunOut, status_code=status.HTTP_202_ACCEPTED)
def trigger_run(
    project_id: int, db: DbSession, user: DeveloperUser, payload: RunCreate | None = None
) -> Run:
    # 202, not 201: the run is only queued here. A worker picks it up and executes it.
    project = _get_or_404(db, project_id)
    ref = payload.ref if payload and payload.ref else project.default_branch
    run = Run(project_id=project.id, ref=ref, triggered_by=user.username)
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


@router.get("/{project_id}/runs", response_model=list[RunOut])
def list_runs(
    project_id: int, db: DbSession, limit: Annotated[int, Query(ge=1, le=200)] = 50
) -> list[Run]:
    """The project's most recent runs, newest first."""
    _get_or_404(db, project_id)
    query = select(Run).where(Run.project_id == project_id).order_by(Run.id.desc()).limit(limit)
    return list(db.scalars(query))


@router.get("/{project_id}/flaky-tests", response_model=list[FlakyTestOut])
def list_flaky_tests(project_id: int, db: DbSession) -> list[FlakyTest]:
    """Tests that both passed and failed on the same commit, most recently flaky first."""
    _get_or_404(db, project_id)
    return find_flaky_tests(db, project_id)


@router.get("/{project_id}/quarantine", response_model=list[QuarantinedTestOut])
def list_quarantined_tests(project_id: int, db: DbSession) -> list[QuarantineEntry]:
    """Quarantined tests, oldest first, each with how it has done since it went in."""
    _get_or_404(db, project_id)
    return list_quarantine(db, project_id)


@router.post(
    "/{project_id}/quarantine",
    response_model=QuarantinedTestOut,
    status_code=status.HTTP_201_CREATED,
)
def quarantine(
    project_id: int, payload: QuarantineCreate, db: DbSession, user: DeveloperUser
) -> QuarantineEntry:
    """Stop a flaky test's failures from failing runs, until it's released."""
    _get_or_404(db, project_id)
    try:
        entry_id = quarantine_test(
            db, project_id, payload.classname, payload.name, payload.reason, user.username
        )
    except NotFlaky:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "no evidence this test is flaky: it has never both passed and failed on one "
            "commit. A test that fails every time is broken, and needs fixing, not quarantine.",
        ) from None
    except AlreadyQuarantined:
        raise HTTPException(status.HTTP_409_CONFLICT, "this test is already quarantined") from None
    return next(entry for entry in list_quarantine(db, project_id) if entry.id == entry_id)


@router.delete("/{project_id}/quarantine/{entry_id}", status_code=status.HTTP_204_NO_CONTENT)
def release(project_id: int, entry_id: int, db: DbSession, user: DeveloperUser) -> None:
    """Take a test out of quarantine: from the next run on, its failures count again."""
    _get_or_404(db, project_id)
    if not release_test(db, project_id, entry_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such test in this project's quarantine")
