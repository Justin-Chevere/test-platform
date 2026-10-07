from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app import security
from app.auth import get_login_guard
from app.db import Base, get_db, make_engine
from app.main import app
from app.models import Project, Role, Run, User
from app.security import create_access_token, hash_password
from app.throttle import LoginGuard


class FakeClock:
    """Time that only moves when a test says so."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture(autouse=True)
def fast_password_hashing(monkeypatch: pytest.MonkeyPatch) -> None:
    # Real hashing costs 64 MiB and tens of milliseconds on purpose. Tests need correctness.
    monkeypatch.setattr(
        security, "_hasher", PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
    )


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def login_guard(clock: FakeClock) -> LoginGuard:
    # A fresh guard per test, so failed logins in one test can't block another.
    return LoginGuard(per_account=3, per_client=5, window_seconds=60, clock=clock)


@pytest.fixture
def session_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    # A fresh database file per test. A file, not memory: workers use several connections
    # at once (each heartbeat thread has its own), and an in-memory SQLite database only
    # exists inside a single connection.
    engine = make_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, autoflush=False)
    engine.dispose()


@pytest.fixture
def anonymous(
    session_factory: sessionmaker[Session], login_guard: LoginGuard
) -> Iterator[TestClient]:
    """A client with no token: someone who hasn't logged in."""

    def override_get_db() -> Iterator[Session]:
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_login_guard] = lambda: login_guard
    # Not used as a context manager on purpose: that would run the lifespan, which checks
    # the real database file. The base URL is localhost, the only host the API answers.
    yield TestClient(app, base_url="http://localhost")
    app.dependency_overrides.clear()


@pytest.fixture
def make_user(session_factory: sessionmaker[Session]) -> Callable[..., User]:
    def _make_user(
        username: str,
        role: Role = Role.VIEWER,
        *,
        password: str = "test-password-123",
        is_active: bool = True,
    ) -> User:
        with session_factory() as db:
            user = User(
                username=username,
                password_hash=hash_password(password),
                role=role,
                is_active=is_active,
            )
            db.add(user)
            db.commit()
            db.refresh(user)
            return user

    return _make_user


@pytest.fixture
def client_as(anonymous: TestClient, make_user: Callable[..., User]) -> Callable[..., TestClient]:
    """Build a client that sends a valid token for a new user with the given role."""

    def _client_as(role: Role, username: str | None = None) -> TestClient:
        user = make_user(username or role.value, role)
        token = create_access_token(user)
        return TestClient(
            app, base_url="http://localhost", headers={"Authorization": f"Bearer {token}"}
        )

    return _client_as


@pytest.fixture
def viewer_client(client_as: Callable[..., TestClient]) -> TestClient:
    return client_as(Role.VIEWER)


@pytest.fixture
def developer_client(client_as: Callable[..., TestClient]) -> TestClient:
    return client_as(Role.DEVELOPER)


@pytest.fixture
def admin_client(client_as: Callable[..., TestClient]) -> TestClient:
    return client_as(Role.ADMIN)


@pytest.fixture
def client(admin_client: TestClient) -> TestClient:
    """A client allowed to do everything. Tests about permissions use the role clients."""
    return admin_client


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
