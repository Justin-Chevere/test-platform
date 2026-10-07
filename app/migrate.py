"""Database schema versions, kept by Alembic. Every change is a migration in migrations/.

Migrations run as their own step, `alembic upgrade head`, never as a side effect of
starting the API or a worker: several processes starting at once would otherwise race to
migrate the same database. Instead, each one checks the schema is current on startup.
"""

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, inspect, text

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


class SchemaOutOfDate(RuntimeError):
    pass


def alembic_config(database_url: str | None = None) -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    if database_url is not None:
        config.attributes["database_url"] = database_url  # read by migrations/env.py
    return config


def upgrade(database_url: str) -> None:
    """Apply every migration the database doesn't have yet."""
    command.upgrade(alembic_config(database_url), "head")


def check_schema(engine: Engine) -> None:
    """Raise SchemaOutOfDate unless the database has every migration applied."""
    latest = set(ScriptDirectory.from_config(alembic_config()).get_heads())
    current = _applied_revision(engine)
    if current != latest:
        raise SchemaOutOfDate(
            f"the database schema is at {sorted(current) or 'no version'}, but the latest "
            f"is {sorted(latest)}. Run `alembic upgrade head` to update it."
        )


def _applied_revision(engine: Engine) -> set[str]:
    # Alembic records how far a database has been migrated in a one-row table of its own.
    with engine.connect() as connection:
        if not inspect(connection).has_table("alembic_version"):
            return set()
        return set(connection.scalars(text("SELECT version_num FROM alembic_version")))
