import io

import pytest
from alembic import command
from sqlalchemy import select

from app.cli import main
from app.migrate import alembic_config
from app.models import Role, User
from app.security import verify_password


@pytest.fixture
def migrated(session_factory):
    """The test database, marked as fully migrated. It was built straight from the models,
    which is what `alembic stamp head` is for."""
    url = session_factory.kw["bind"].url.render_as_string(hide_password=False)
    command.stamp(alembic_config(url), "head")
    return session_factory


def _cli(session_factory, monkeypatch, *argv, password):
    monkeypatch.setattr("sys.stdin", io.StringIO(password + "\n"))
    return main([*argv, "--password-stdin"], session_factory=session_factory)


def _user(session_factory, username) -> User:
    with session_factory() as db:
        return db.scalar(select(User).where(User.username == username))


def test_create_the_first_admin(migrated, monkeypatch, capsys):
    code = _cli(
        migrated, monkeypatch, "create-user", "alice", "--role", "admin", password="alice-pw-1234"
    )

    assert code == 0
    user = _user(migrated, "alice")
    assert user.role == Role.ADMIN
    assert verify_password("alice-pw-1234", user.password_hash)
    assert "created admin user alice" in capsys.readouterr().out


def test_a_weak_password_is_refused_without_echoing_it(migrated, monkeypatch, capsys):
    code = _cli(migrated, monkeypatch, "create-user", "alice", password="hunter2")

    assert code == 1
    error = capsys.readouterr().err
    assert "password" in error
    assert "hunter2" not in error
    assert _user(migrated, "alice") is None


def test_an_existing_username_is_refused(migrated, monkeypatch, make_user, capsys):
    make_user("alice")

    code = _cli(migrated, monkeypatch, "create-user", "alice", password="alice-pw-1234")

    assert code == 1
    assert "already exists" in capsys.readouterr().err


def test_set_password_ends_existing_sessions(migrated, monkeypatch, make_user):
    make_user("alice", password="old-password-123")

    code = _cli(migrated, monkeypatch, "set-password", "alice", password="new-password-456")

    assert code == 0
    user = _user(migrated, "alice")
    assert verify_password("new-password-456", user.password_hash)
    assert user.token_version == 1  # tokens issued before carry version 0: retired


def test_set_password_for_someone_who_does_not_exist(migrated, monkeypatch, capsys):
    code = _cli(migrated, monkeypatch, "set-password", "ghost", password="new-password-456")

    assert code == 1
    assert "no user 'ghost'" in capsys.readouterr().err


def test_refuses_a_database_that_is_not_migrated(session_factory, monkeypatch, capsys):
    code = _cli(session_factory, monkeypatch, "create-user", "alice", password="alice-pw-1234")

    assert code == 1
    assert "alembic upgrade head" in capsys.readouterr().err
