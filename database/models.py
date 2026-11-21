"""
Modelos relacionales SQLAlchemy 2.0 para Supabase PostgreSQL (database/models.py).

Implementa el esquema exigido en la Sección 9:
- `poems`: Registro maestro de cada ciclo poético de 60 minutos.
- `poem_versions`: Versiones completas e inmutables por cada intervención (1..6),
  con restricción UNIQUE(poem_id, intervention_number).
- `agent_logs`: Bitácora cronológica de decisiones, transiciones, peticiones y errores
  con columna `details` en JSONB.
- `agent_state`: Estado persistente del agente autónomo y bloqueo transaccional de ciclo activo.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from config import utc_now


class Base(DeclarativeBase):
    """Clase base declarativa para los modelos SQLAlchemy de POETA."""


# Estados en los que un poema se considera ACTIVO (bloquea la creación de otro ciclo)
ACTIVE_POEM_STATUSES: tuple[str, ...] = (
    "INITIALIZING",
    "RUNNING",
    "GENERATING",
    "RECOVERING",
    "ERROR",
)

# Singleton UUID fijo para la fila de control global en `agent_state`
SINGLETON_AGENT_STATE_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


class Poem(Base):
    """
    Tabla `poems`: Representa una composición poética y su ciclo de trabajo de 60 minutos.
    """

    __tablename__ = "poems"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    theme: Mapped[str] = mapped_column(String(500), nullable=False)
    initial_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    full_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    total_interventions: Mapped[int] = mapped_column(Integer, nullable=False, default=6)
    completed_interventions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    scheduled_end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    next_execution_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        index=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )

    versions: Mapped[list["PoemVersion"]] = relationship(
        "PoemVersion",
        back_populates="poem",
        order_by="PoemVersion.intervention_number.asc()",
        cascade="all, delete-orphan",
    )
    logs: Mapped[list["AgentLog"]] = relationship(
        "AgentLog",
        back_populates="poem",
        order_by="AgentLog.created_at.asc()",
    )

    __table_args__ = (
        Index("idx_poems_status_started_at", "status", "started_at"),
        # Índice único parcial en PostgreSQL que impide tener >1 poema activo simultáneamente
        Index(
            "idx_single_active_poem",
            text("(1)"),
            unique=True,
            postgresql_where=text(
                "status IN ('INITIALIZING', 'RUNNING', 'GENERATING', 'RECOVERING', 'ERROR')"
            ),
            sqlite_where=text(
                "status IN ('INITIALIZING', 'RUNNING', 'GENERATING', 'RECOVERING', 'ERROR')"
            ),
        ),
    )


class PoemVersion(Base):
    """
    Tabla `poem_versions`: Almacena cada versión completa e inmutable del poema,
    junto con la estrofa agregada, el snapshot del prompt y el tiempo de generación.
    """

    __tablename__ = "poem_versions"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    poem_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("poems.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    intervention_number: Mapped[int] = mapped_column(Integer, nullable=False)
    added_stanza: Mapped[str] = mapped_column(Text, nullable=False)
    full_text: Mapped[str] = mapped_column(Text, nullable=False)
    model_name: Mapped[str] = mapped_column(String(120), nullable=False)
    prompt_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        index=True,
    )
    generation_duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    generation_status: Mapped[str] = mapped_column(String(50), nullable=False, default="CONFIRMED")

    poem: Mapped["Poem"] = relationship("Poem", back_populates="versions")

    __table_args__ = (
        UniqueConstraint(
            "poem_id",
            "intervention_number",
            name="uq_poem_versions_poem_intervention",
        ),
        Index("idx_poem_versions_poem_num", "poem_id", "intervention_number"),
    )


class AgentLog(Base):
    """
    Tabla `agent_logs`: Registro cronológico de actividades, transiciones de estado,
    peticiones a Ollama Cloud, recuperaciones de memoria y errores.
    """

    __tablename__ = "agent_logs"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    poem_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("poems.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    event_type: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    details: Mapped[dict[str, Any] | None] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        index=True,
    )

    poem: Mapped["Poem | None"] = relationship("Poem", back_populates="logs")

    __table_args__ = (Index("idx_agent_logs_poem_created", "poem_id", "created_at"),)


class AgentState(Base):
    """
    Tabla `agent_state`: Almacena el estado global de la máquina de estados del agente
    y sirve como punto de sincronización transaccional entre procesos.
    """

    __tablename__ = "agent_state"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=lambda: SINGLETON_AGENT_STATE_ID,
    )
    agent_status: Mapped[str] = mapped_column(String(50), nullable=False, default="IDLE")
    active_poem_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("poems.id", ondelete="SET NULL"),
        nullable=True,
    )
    last_execution_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    next_execution_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    last_intervention_number: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )
