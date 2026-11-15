"""release records and the triple-store sink queue (FR-GIT-06, FR-TS)

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "releases",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("repo", sa.String(64), nullable=False),
        sa.Column("model", sa.String(255), nullable=False),
        sa.Column("version", sa.String(64), nullable=False),
        sa.Column("tag", sa.String(320), nullable=False),
        sa.Column("commit_sha", sa.String(64), nullable=False),
        sa.Column("released_by", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("sink_status", sa.String(16), nullable=False),
        sa.Column("sink_attempts", sa.Integer(), nullable=False),
        sa.Column("sink_next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sink_last_error", sa.String(500), nullable=True),
        sa.UniqueConstraint("repo", "tag", name="uq_releases_repo_tag"),
    )
    op.create_index("ix_releases_repo_sink_status", "releases", ["repo", "sink_status"])


def downgrade() -> None:
    op.drop_index("ix_releases_repo_sink_status", table_name="releases")
    op.drop_table("releases")
