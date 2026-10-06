from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import DbSession
from app.models import Project, Run
from app.schemas import ProjectCreate, ProjectOut, RunCreate, RunOut

router = APIRouter(prefix="/projects", tags=["projects"])


def _get_or_404(db: Session, project_id: int) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "project not found")
    return project


@router.post("", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
def create_project(payload: ProjectCreate, db: DbSession) -> Project:
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
def trigger_run(project_id: int, db: DbSession, payload: RunCreate | None = None) -> Run:
    # 202, not 201: the run is only queued here. A worker picks it up and executes it.
    project = _get_or_404(db, project_id)
    ref = payload.ref if payload and payload.ref else project.default_branch
    run = Run(project_id=project.id, ref=ref)
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
