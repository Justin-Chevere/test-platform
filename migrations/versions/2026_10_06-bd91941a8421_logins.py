"""Logins: users with roles, and who triggered each run and quarantined each test.

Runs and quarantine entries from before this migration name nobody (NULL).

Revision ID: bd91941a8421
Revises: 16053db999d8
Create Date: 2026-10-06 22:01:40.031350
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Revision identifiers, used by Alembic.
revision: str = "bd91941a8421"
down_revision: str | Sequence[str] | None = "16053db999d8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("username", sa.String(length=32), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column(
            "role",
            sa.Enum("viewer", "developer", "admin", name="role", native_enum=False, length=16),
            nullable=False,
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("token_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("username", name=op.f("uq_users_username")),
    )
    with op.batch_alter_table("quarantined_tests", schema=None) as batch_op:
        batch_op.add_column(sa.Column("created_by", sa.String(length=32), nullable=True))

    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("triggered_by", sa.String(length=32), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.drop_column("triggered_by")

    with op.batch_alter_table("quarantined_tests", schema=None) as batch_op:
        batch_op.drop_column("created_by")

    op.drop_table("users")
