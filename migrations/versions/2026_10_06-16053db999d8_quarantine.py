"""Quarantine: flaky tests whose failures don't fail runs.

Runs count the failures they let through, and each test result records whether it was
quarantined at the time. Existing rows get 0 and false.

Revision ID: 16053db999d8
Revises: 7cfb95761266
Create Date: 2026-10-06 21:08:41.968967
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Revision identifiers, used by Alembic.
revision: str = "16053db999d8"
down_revision: str | Sequence[str] | None = "7cfb95761266"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "quarantined_tests",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("classname", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], name=op.f("fk_quarantined_tests_project_id_projects")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_quarantined_tests")),
        sa.UniqueConstraint(
            "project_id",
            "classname",
            "name",
            name=op.f("uq_quarantined_tests_project_id_classname_name"),
        ),
    )
    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("tests_quarantined", sa.Integer(), server_default="0", nullable=False)
        )

    with op.batch_alter_table("test_results", schema=None) as batch_op:
        # sa.false(), not the sa.text("0") autogenerate wrote: that's SQLite's spelling,
        # and Postgres refuses 0 as the default of a boolean column.
        batch_op.add_column(
            sa.Column("quarantined", sa.Boolean(), server_default=sa.false(), nullable=False)
        )


def downgrade() -> None:
    with op.batch_alter_table("test_results", schema=None) as batch_op:
        batch_op.drop_column("quarantined")

    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.drop_column("tests_quarantined")

    op.drop_table("quarantined_tests")
