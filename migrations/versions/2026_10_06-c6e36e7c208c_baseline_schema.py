"""Baseline: projects, runs and test results, as they were before migrations existed.

Revision ID: c6e36e7c208c
Revises:
Create Date: 2026-10-06 20:13:39.103012
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Revision identifiers, used by Alembic.
revision: str = "c6e36e7c208c"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "projects",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=63), nullable=False),
        sa.Column("repo_url", sa.String(length=255), nullable=False),
        sa.Column("default_branch", sa.String(length=255), nullable=False),
        sa.Column("setup_command", sa.String(length=1000), nullable=False),
        sa.Column("test_command", sa.String(length=1000), nullable=False),
        sa.Column("report_path", sa.String(length=255), nullable=False),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_projects")),
        sa.UniqueConstraint("name", name=op.f("uq_projects_name")),
    )
    op.create_table(
        "runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("ref", sa.String(length=255), nullable=False),
        sa.Column("commit_sha", sa.String(length=64), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "queued",
                "running",
                "passed",
                "failed",
                "error",
                name="runstatus",
                native_enum=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("worker_id", sa.String(length=255), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(), nullable=True),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("tests_passed", sa.Integer(), nullable=False),
        sa.Column("tests_failed", sa.Integer(), nullable=False),
        sa.Column("tests_errored", sa.Integer(), nullable=False),
        sa.Column("tests_skipped", sa.Integer(), nullable=False),
        sa.Column("log", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], name=op.f("fk_runs_project_id_projects")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_runs")),
    )
    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_runs_project_id"), ["project_id"], unique=False)
        batch_op.create_index("ix_runs_status_id", ["status", "id"], unique=False)

    op.create_table(
        "test_results",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("classname", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "outcome",
            sa.Enum(
                "passed", "failed", "error", "skipped", name="outcome", native_enum=False, length=16
            ),
            nullable=False,
        ),
        sa.Column("duration_seconds", sa.Double(), nullable=False),
        sa.Column("message", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], name=op.f("fk_test_results_run_id_runs")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_test_results")),
    )
    with op.batch_alter_table("test_results", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_test_results_run_id"), ["run_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("test_results", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_test_results_run_id"))

    op.drop_table("test_results")
    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.drop_index("ix_runs_status_id")
        batch_op.drop_index(batch_op.f("ix_runs_project_id"))

    op.drop_table("runs")
    op.drop_table("projects")
