import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine

from app.db import Base
from app.migrate import SchemaOutOfDate, alembic_config, check_schema, upgrade


@pytest.fixture
def database_url(tmp_path) -> str:
    return f"sqlite:///{tmp_path / 'migrated.db'}"


def _differences_from_models(database_url: str) -> list:
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            return compare_metadata(MigrationContext.configure(connection), Base.metadata)
    finally:
        engine.dispose()


def test_migrations_build_exactly_the_schema_the_models_describe(database_url):
    # The other tests create tables straight from the models, which is faster. This test
    # makes sure that's the same schema the migrations build: a model changed without a
    # migration shows up here as a difference.
    upgrade(database_url)

    assert _differences_from_models(database_url) == []


def test_every_migration_can_be_undone_and_redone(database_url):
    config = alembic_config(database_url)

    command.upgrade(config, "head")
    command.downgrade(config, "base")
    command.upgrade(config, "head")

    assert _differences_from_models(database_url) == []


def test_startup_refuses_a_database_that_is_not_migrated(database_url):
    engine = create_engine(database_url)
    try:
        with pytest.raises(SchemaOutOfDate, match="alembic upgrade head"):
            check_schema(engine)

        upgrade(database_url)

        check_schema(engine)  # no complaint once every migration is applied
    finally:
        engine.dispose()
