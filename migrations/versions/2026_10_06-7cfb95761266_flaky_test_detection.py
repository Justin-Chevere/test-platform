"""Flaky test detection: each rerun points to the first run of its commit.

Projects also choose how many automatic reruns a failed run gets; existing ones get 1.

Revision ID: 7cfb95761266
Revises: c6e36e7c208c
Create Date: 2026-10-06 20:15:31.595723
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Revision identifiers, used by Alembic.
revision: str = "7cfb95761266"
down_revision: str | Sequence[str] | None = "c6e36e7c208c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("auto_reruns", sa.Integer(), server_default="1", nullable=False)
        )

    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("rerun_of_id", sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f("ix_runs_rerun_of_id"), ["rerun_of_id"], unique=False)
        batch_op.create_foreign_key(
            batch_op.f("fk_runs_rerun_of_id_runs"), "runs", ["rerun_of_id"], ["id"]
        )


def downgrade() -> None:
    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f("fk_runs_rerun_of_id_runs"), type_="foreignkey")
        batch_op.drop_index(batch_op.f("ix_runs_rerun_of_id"))
        batch_op.drop_column("rerun_of_id")

    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.drop_column("auto_reruns")
