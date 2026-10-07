"""Applies migrations: through the alembic command (`alembic upgrade head`) or app.migrate.

The database URL comes from the app's own settings, so `alembic` always works on the same
database as the app, unless code passes one in config.attributes.
"""

import logging
from typing import Literal

from alembic import context
from alembic.autogenerate.api import AutogenContext
from sqlalchemy import create_engine, pool

from app import models  # also registers every table on Base.metadata
from app.config import get_settings
from app.db import Base

config = context.config
url = config.attributes.get("database_url") or get_settings().database_url

# Say what the alembic command is doing, without SQLAlchemy echoing every statement.
logging.basicConfig(format="%(levelname)-5.5s [%(name)s] %(message)s")
logging.getLogger("alembic").setLevel(logging.INFO)


def render_item(type_: str, obj: object, _context: AutogenContext) -> str | Literal[False]:
    """Write column types for new migrations without referring to app code.

    A migration describes the database as it was when the migration was written, and app
    code keeps changing after that. UTCDateTime is a plain DateTime in the database.
    """
    if type_ == "type" and isinstance(obj, models.UTCDateTime):
        return "sa.DateTime()"
    return False  # anything else: the default rendering


def run_migrations_offline() -> None:
    """Print the SQL instead of running it: `alembic upgrade head --sql`."""
    context.configure(
        url=url,
        target_metadata=Base.metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
        render_item=render_item,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # A plain engine rather than app.db's, which turns on SQLite's foreign key checks:
    # changing a table in SQLite means copying it and dropping the old one, and with
    # those checks on, the drop fails as soon as another table points at it.
    engine = create_engine(url, poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=Base.metadata,
            # SQLite's ALTER TABLE can't change or drop columns. Batch mode copies the
            # table instead; on other databases it's a plain ALTER TABLE.
            render_as_batch=True,
            render_item=render_item,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
