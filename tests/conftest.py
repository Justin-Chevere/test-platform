from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db, make_engine
from app.main import app
from app.models import Project, Run


@pytest.fixture
def session_factory() -> sessionmaker[Session]:
    # A fresh in-memory database per test. StaticPool keeps one connection alive,
    # otherwise every new connection would see its own empty in-memory database.
    engine = make_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False)


@pytest.fixture
def client(session_factory: sessionmaker[Session]) -> Iterator[TestClient]:
    def override_get_db() -> Iterator[Session]:
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    # Not used as a context manager on purpose: that would run the lifespan, which
    # creates the real database file. The base URL is localhost, the only host it answers.
    yield TestClient(app, base_url="http://localhost")
    app.dependency_overrides.clear()


@pytest.fixture
def make_project(session_factory: sessionmaker[Session]) -> Callable[..., Project]:
    def _make_project(name: str = "demo", **overrides: Any) -> Project:
        fields = {
            "name": name,
            "repo_url": "https://github.com/example/demo",
            "default_branch": "main",
            "setup_command": "",
            "test_command": "python -m pytest --junitxml=test-report.xml",
            "report_path": "test-report.xml",
            "timeout_seconds": 600,
        }
        with session_factory() as db:
            project = Project(**(fields | overrides))
            db.add(project)
            db.commit()
            db.refresh(project)
            return project

    return _make_project


@pytest.fixture
def queue_run(session_factory: sessionmaker[Session]) -> Callable[..., int]:
    """Add a queued run for a project and return its id."""

    def _queue_run(project: Project, ref: str = "main") -> int:
        with session_factory() as db:
            run = Run(project_id=project.id, ref=ref)
            db.add(run)
            db.commit()
            return run.id

    return _queue_run
