"""Command line for what the API can't do for itself: create the first admin, and set a
password for someone who has lost theirs.

    python -m app.cli create-user alice --role admin
    python -m app.cli set-password alice
"""

import argparse
import getpass
import sys
from collections.abc import Callable

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.migrate import SchemaOutOfDate, check_schema
from app.models import Role, User
from app.schemas import Password, UserCreate
from app.security import hash_password


def main(
    argv: list[str] | None = None, session_factory: Callable[[], Session] = SessionLocal
) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create-user", help="create a user, such as the first admin")
    create.add_argument("username")
    create.add_argument("--role", choices=[role.value for role in Role], default=Role.VIEWER)
    reset = commands.add_parser(
        "set-password", help="set a user's password, e.g. a lost one; their sessions end"
    )
    reset.add_argument("username")
    for command in (create, reset):
        command.add_argument(
            "--password-stdin",
            action="store_true",
            help="read the password from stdin instead of prompting (for scripts)",
        )
    args = parser.parse_args(argv)

    password = _read_password(args.password_stdin)
    if password is None:
        return 1
    with session_factory() as db:
        try:
            check_schema(db.get_bind())
        except SchemaOutOfDate as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        if args.command == "create-user":
            return _create_user(db, args.username, password, args.role)
        return _set_password(db, args.username, password)


def _read_password(from_stdin: bool) -> str | None:
    if from_stdin:
        return sys.stdin.readline().rstrip("\r\n")
    password = getpass.getpass("Password: ")
    if password != getpass.getpass("Repeat password: "):
        print("passwords do not match", file=sys.stderr)
        return None
    return password


def _create_user(db: Session, username: str, password: str, role: str) -> int:
    try:
        # The same rules POST /users applies.
        payload = UserCreate(username=username, password=password, role=role)
    except ValidationError as exc:
        _print_errors(exc)
        return 1
    db.add(
        User(
            username=payload.username,
            password_hash=hash_password(payload.password),
            role=payload.role,
        )
    )
    try:
        db.commit()
    except IntegrityError:
        print(f"user {username!r} already exists", file=sys.stderr)
        return 1
    print(f"created {payload.role} user {payload.username}")
    return 0


def _set_password(db: Session, username: str, password: str) -> int:
    try:
        TypeAdapter(Password).validate_python(password)
    except ValidationError as exc:
        _print_errors(exc)
        return 1
    user = db.scalar(select(User).where(User.username == username))
    if user is None:
        print(f"no user {username!r}", file=sys.stderr)
        return 1
    user.password_hash = hash_password(password)
    user.token_version += 1  # whoever might hold their old sessions is logged out
    db.commit()
    print(f"set a new password for {username}; their other sessions have ended")
    return 0


def _print_errors(exc: ValidationError) -> None:
    # include_input=False: never echo the password back into the terminal.
    for error in exc.errors(include_input=False, include_url=False):
        field = error["loc"][0] if error["loc"] else "password"
        print(f"{field}: {error['msg']}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
