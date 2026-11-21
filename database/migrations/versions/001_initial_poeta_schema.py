"""Migración inicial 001: Creación de tablas poems, poem_versions, agent_logs y agent_state.

Revision ID: 001_initial_poeta
Revises: None
Create Date: 2026-11-21
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "001_initial_poeta"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "poems",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("theme", sa.String(length=500), nullable=False),
        sa.Column("initial_prompt", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("full_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("total_interventions", sa.Integer(), nullable=False, server_default="6"),
        sa.Column("completed_interventions", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("scheduled_end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("next_execution_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_poems_status", "poems", ["status"])
    op.create_index("ix_poems_next_execution_at", "poems", ["next_execution_at"])
    op.create_index("ix_poems_created_at", "poems", ["created_at"])
    op.create_index("idx_poems_status_started_at", "poems", ["status", "started_at"])
    op.create_index(
        "idx_single_active_poem",
        "poems",
        [sa.text("(1)")],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('INITIALIZING', 'RUNNING', 'GENERATING', 'RECOVERING', 'ERROR')"
        ),
        sqlite_where=sa.text(
            "status IN ('INITIALIZING', 'RUNNING', 'GENERATING', 'RECOVERING', 'ERROR')"
        ),
    )

    op.create_table(
        "poem_versions",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "poem_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("poems.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("intervention_number", sa.Integer(), nullable=False),
        sa.Column("added_stanza", sa.Text(), nullable=False),
        sa.Column("full_text", sa.Text(), nullable=False),
        sa.Column("model_name", sa.String(length=120), nullable=False),
        sa.Column("prompt_snapshot", sa.Text(), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("generation_duration_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("generation_status", sa.String(length=50), nullable=False, server_default="CONFIRMED"),
        sa.UniqueConstraint("poem_id", "intervention_number", name="uq_poem_versions_poem_intervention"),
    )
    op.create_index("ix_poem_versions_poem_id", "poem_versions", ["poem_id"])
    op.create_index("ix_poem_versions_generated_at", "poem_versions", ["generated_at"])
    op.create_index("idx_poem_versions_poem_num", "poem_versions", ["poem_id", "intervention_number"])

    op.create_table(
        "agent_logs",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "poem_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("poems.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("event_type", sa.String(length=80), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column(
            "details",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_agent_logs_poem_id", "agent_logs", ["poem_id"])
    op.create_index("ix_agent_logs_event_type", "agent_logs", ["event_type"])
    op.create_index("ix_agent_logs_status", "agent_logs", ["status"])
    op.create_index("ix_agent_logs_created_at", "agent_logs", ["created_at"])
    op.create_index("idx_agent_logs_poem_created", "agent_logs", ["poem_id", "created_at"])

    op.create_table(
        "agent_state",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("agent_status", sa.String(length=50), nullable=False, server_default="IDLE"),
        sa.Column(
            "active_poem_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("poems.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("last_execution_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_execution_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_intervention_number", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("agent_state")
    op.drop_table("agent_logs")
    op.drop_table("poem_versions")
    op.drop_table("poems")
