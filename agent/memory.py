"""
Repositorio de Memoria Persistente de POETA (agent/memory.py).

Centraliza todas las lecturas y escrituras en Supabase PostgreSQL.
No utiliza variables globales ni listas en RAM para almacenar el contexto del poema.

Responsabilidades:
- Recuperar la memoria completa del poema activo desde PostgreSQL antes de cada intervención.
- Garantizar la exclusividad de un único poema activo simultáneamente (validación lógica +
  bloqueo transaccional + índice único parcial en base de datos).
- Almacenar cada versión completa e inmutable del poema junto con la estrofa agregada.
- Impedir duplicados mediante la restricción UNIQUE(poem_id, intervention_number).
- Registrar toda la trazabilidad de eventos con hora de creación, hora programada,
  hora real de ejecución y hora de finalización.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
import uuid

from sqlalchemy import desc, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from config import ensure_utc, to_bogota_iso, to_utc_iso, utc_now
from database.connection import DatabaseManager
from database.models import (
    ACTIVE_POEM_STATUSES,
    AgentLog,
    AgentState,
    Poem,
    PoemVersion,
)
from agent.state_manager import AgentStatus, StateManager


class ActiveCycleAlreadyExistsError(RuntimeError):
    """Excepción lanzada cuando se intenta crear un ciclo mientras existe otro activo."""


class DuplicateInterventionError(RuntimeError):
    """Excepción lanzada cuando se intenta registrar una intervención ya existente."""


class PoemNotFoundError(LookupError):
    """Excepción lanzada cuando no se encuentra el poema solicitado en la base de datos."""


@dataclass(frozen=True)
class PersistentPoemMemory:
    """
    Instantánea completa de la memoria persistente recuperada desde PostgreSQL
    para continuar el desarrollo literario del poema.
    """

    poem_id: uuid.UUID
    title: str
    theme: str
    initial_prompt: str
    status: str
    full_text: str
    total_interventions: int
    completed_interventions: int
    started_at: datetime
    scheduled_end_at: datetime
    next_execution_at: datetime | None
    completed_at: datetime | None
    stanzas: list[str]
    last_version_number: int


class PersistentMemoryRepository:
    """Repositorio transaccional de memoria persistente respaldado por PostgreSQL."""

    def __init__(self, db_manager: DatabaseManager) -> None:
        self.db = db_manager

    def get_active_poem(self, session: Session, lock_for_update: bool = False) -> Poem | None:
        """Consulta en PostgreSQL si existe un poema activo en este momento."""
        stmt = (
            select(Poem)
            .where(Poem.status.in_(ACTIVE_POEM_STATUSES))
            .order_by(Poem.started_at.desc())
        )
        if lock_for_update:
            stmt = stmt.with_for_update()
        return session.execute(stmt).scalars().first()

    def recover_poem_memory(
        self,
        session: Session,
        poem_id: uuid.UUID | None = None,
        *,
        record_recovery_log: bool = True,
    ) -> PersistentPoemMemory | None:
        """
        Recupera desde PostgreSQL toda la memoria persistente de un poema (el activo
        o el indicado por `poem_id`), incluyendo sus versiones confirmadas en orden cronológico.
        """
        if poem_id is not None:
            poem = session.execute(select(Poem).where(Poem.id == poem_id)).scalar_one_or_none()
        else:
            poem = self.get_active_poem(session)

        if poem is None:
            return None

        versions_stmt = (
            select(PoemVersion)
            .where(
                PoemVersion.poem_id == poem.id,
                PoemVersion.generation_status == "CONFIRMED",
            )
            .order_by(PoemVersion.intervention_number.asc())
        )
        versions = list(session.execute(versions_stmt).scalars().all())

        stanzas = [v.added_stanza.strip() for v in versions if v.added_stanza and v.added_stanza.strip()]
        last_version_num = versions[-1].intervention_number if versions else 0
        reconstructed_full_text = (
            versions[-1].full_text if versions else (poem.full_text or "")
        )

        # Sincronizar contadores si hubo un corte abrupto previo
        if len(versions) != poem.completed_interventions or poem.full_text != reconstructed_full_text:
            poem.completed_interventions = len(versions)
            poem.full_text = reconstructed_full_text
            poem.updated_at = utc_now()
            session.flush()

        memory = PersistentPoemMemory(
            poem_id=poem.id,
            title=poem.title,
            theme=poem.theme,
            initial_prompt=poem.initial_prompt,
            status=poem.status,
            full_text=reconstructed_full_text,
            total_interventions=poem.total_interventions,
            completed_interventions=len(versions),
            started_at=ensure_utc(poem.started_at) or utc_now(),
            scheduled_end_at=ensure_utc(poem.scheduled_end_at) or utc_now(),
            next_execution_at=ensure_utc(poem.next_execution_at),
            completed_at=ensure_utc(poem.completed_at),
            stanzas=stanzas,
            last_version_number=last_version_num,
        )

        if record_recovery_log:
            self.log_event(
                session,
                poem_id=poem.id,
                event_type="MEMORY_RECOVERED",
                description=(
                    f"Memoria persistente recuperada para '{poem.title}' "
                    f"({memory.completed_interventions}/{memory.total_interventions} intervenciones confirmadas)."
                ),
                status="SUCCESS",
                details={
                    "title": poem.title,
                    "theme": poem.theme,
                    "completed_interventions": memory.completed_interventions,
                    "last_version_number": memory.last_version_number,
                    "stanzas_count": len(stanzas),
                },
            )

        return memory

    def initialize_cycle_record(
        self,
        session: Session,
        *,
        initial_prompt: str,
        total_interventions: int,
        started_at: datetime,
        scheduled_end_at: datetime,
        next_execution_at: datetime,
    ) -> Poem:
        """
        Verifica en la base de datos que no exista ningún otro poema activo (con bloqueo
        transaccional sobre `agent_state` e índice único parcial en `poems`) y crea el
        registro inicial del ciclo en estado INITIALIZING.
        """
        # 1. Bloquear la fila singleton de estado para serializar inicios concurrentes
        _ = StateManager.get_or_create_state(session, lock_for_update=True)

        # 2. Verificar que no exista un poema activo en la tabla poems
        existing_active = self.get_active_poem(session, lock_for_update=True)
        if existing_active is not None:
            raise ActiveCycleAlreadyExistsError(
                f"Ya existe una composición poética activa ('{existing_active.title}', "
                f"ID: {existing_active.id}, estado: {existing_active.status}). "
                "No se permite más de un ciclo activo simultáneamente."
            )

        now = ensure_utc(started_at) or utc_now()
        poem = Poem(
            id=uuid.uuid4(),
            title="Composición en preparación...",
            theme="Determinando temática lírica...",
            initial_prompt=initial_prompt,
            status=AgentStatus.INITIALIZING.value,
            full_text="",
            total_interventions=total_interventions,
            completed_interventions=0,
            started_at=now,
            scheduled_end_at=ensure_utc(scheduled_end_at) or now,
            next_execution_at=ensure_utc(next_execution_at),
            completed_at=None,
            created_at=now,
            updated_at=now,
        )
        session.add(poem)
        try:
            session.flush()
        except IntegrityError as exc:
            raise ActiveCycleAlreadyExistsError(
                "Condición de carrera detectada en PostgreSQL: ya existe otro ciclo poético activo."
            ) from exc

        StateManager.transition(
            session,
            AgentStatus.INITIALIZING,
            poem_id=poem.id,
            reason="Nuevo ciclo de creación literaria registrado.",
            last_execution_at=now,
            next_execution_at=next_execution_at,
            update_next_execution=True,
            last_intervention_number=0,
        )

        self.log_event(
            session,
            poem_id=poem.id,
            event_type="CYCLE_CREATED",
            description="Ciclo de 60 minutos creado e inicializado en la base de datos.",
            status="INITIALIZING",
            scheduled_at=started_at,
            actual_execution_at=now,
            details={
                "total_interventions": total_interventions,
                "started_at_utc": to_utc_iso(started_at),
                "started_at_bogota": to_bogota_iso(started_at),
                "scheduled_end_at_utc": to_utc_iso(scheduled_end_at),
                "scheduled_end_at_bogota": to_bogota_iso(scheduled_end_at),
            },
        )
        return poem

    def has_confirmed_intervention(
        self,
        session: Session,
        poem_id: uuid.UUID,
        intervention_number: int,
    ) -> bool:
        """Comprueba si la intervención indicada ya fue confirmada y almacenada."""
        stmt = select(func.count(PoemVersion.id)).where(
            PoemVersion.poem_id == poem_id,
            PoemVersion.intervention_number == intervention_number,
        )
        count = session.execute(stmt).scalar_one()
        return int(count or 0) > 0

    def save_confirmed_intervention(
        self,
        session: Session,
        *,
        poem: Poem,
        intervention_number: int,
        added_stanza: str,
        full_text: str,
        model_name: str,
        prompt_snapshot: str,
        generation_duration_ms: int,
        next_execution_at: datetime | None,
        title: str | None = None,
        theme: str | None = None,
        scheduled_at: datetime | None = None,
        execution_started_at: datetime | None = None,
    ) -> PoemVersion:
        """
        Guarda de forma atómica e idempotente una nueva versión completa del poema
        y actualiza el registro maestro en `poems` y `agent_state`.
        """
        if self.has_confirmed_intervention(session, poem.id, intervention_number):
            raise DuplicateInterventionError(
                f"La intervención #{intervention_number} del poema {poem.id} ya fue confirmada previamente."
            )

        now = utc_now()
        if title is not None:
            poem.title = title
        if theme is not None:
            poem.theme = theme

        version = PoemVersion(
            id=uuid.uuid4(),
            poem_id=poem.id,
            intervention_number=intervention_number,
            added_stanza=added_stanza,
            full_text=full_text,
            model_name=model_name,
            prompt_snapshot=prompt_snapshot,
            generated_at=now,
            generation_duration_ms=generation_duration_ms,
            generation_status="CONFIRMED",
        )
        session.add(version)
        try:
            session.flush()
        except IntegrityError as exc:
            raise DuplicateInterventionError(
                f"Restricción UNIQUE activada: la intervención #{intervention_number} ya existe para el poema {poem.id}."
            ) from exc

        poem.full_text = full_text
        poem.completed_interventions = intervention_number
        poem.status = AgentStatus.RUNNING.value
        poem.next_execution_at = ensure_utc(next_execution_at)
        poem.updated_at = now
        session.flush()

        self.log_event(
            session,
            poem_id=poem.id,
            event_type="STANZA_GENERATED",
            description=(
                f"Estrofa #{intervention_number} generada con éxito mediante {model_name} "
                f"({generation_duration_ms} ms)."
            ),
            status="SUCCESS",
            scheduled_at=scheduled_at or execution_started_at or now,
            actual_execution_at=execution_started_at or now,
            finished_at=now,
            details={
                "intervention_number": intervention_number,
                "model_name": model_name,
                "generation_duration_ms": generation_duration_ms,
                "added_stanza_preview": added_stanza[:140],
            },
        )

        self.log_event(
            session,
            poem_id=poem.id,
            event_type="VERSION_STORED",
            description=(
                f"Versión #{intervention_number} de '{poem.title}' almacenada de forma inmutable en Supabase."
            ),
            status="CONFIRMED",
            scheduled_at=scheduled_at or execution_started_at or now,
            actual_execution_at=execution_started_at or now,
            finished_at=now,
            details={
                "version_id": str(version.id),
                "intervention_number": intervention_number,
                "total_interventions": poem.total_interventions,
                "next_execution_at_utc": to_utc_iso(next_execution_at),
                "next_execution_at_bogota": to_bogota_iso(next_execution_at),
            },
        )

        StateManager.transition(
            session,
            AgentStatus.RUNNING,
            poem_id=poem.id,
            reason=f"Intervención {intervention_number}/{poem.total_interventions} completada.",
            last_execution_at=now,
            next_execution_at=next_execution_at,
            update_next_execution=True,
            last_intervention_number=intervention_number,
        )

        self.log_event(
            session,
            poem_id=poem.id,
            event_type="INTERVENTION_COMPLETED",
            description=(
                f"Intervención {intervention_number} de {poem.total_interventions} confirmada. "
                + (
                    f"Próxima ejecución programada: {to_bogota_iso(next_execution_at)}."
                    if next_execution_at
                    else "Esperando cierre del ciclo al minuto 60."
                )
            ),
            status="RUNNING",
            scheduled_at=scheduled_at or execution_started_at or now,
            actual_execution_at=execution_started_at or now,
            finished_at=now,
            details={
                "intervention_number": intervention_number,
                "completed_interventions": poem.completed_interventions,
                "total_interventions": poem.total_interventions,
                "next_execution_at_utc": to_utc_iso(next_execution_at),
                "next_execution_at_bogota": to_bogota_iso(next_execution_at),
            },
        )

        return version

    def finalize_and_archive_poem(
        self,
        session: Session,
        poem: Poem,
        *,
        reason: str = "Ciclo de 60 minutos completado y archivado automáticamente.",
    ) -> Poem:
        """
        Finaliza y archiva el poema al cumplirse los 60 minutos (Sección 7 - Cierre):
        - Actualiza el estado del poema y del agente a COMPLETED.
        - Registra fecha y hora de finalización (`completed_at`).
        - Limpia `next_execution_at` y libera el bloqueo de ejecución (`active_poem_id = None`).
        """
        now = utc_now()
        scheduled_close = ensure_utc(poem.scheduled_end_at) or now
        poem.status = AgentStatus.COMPLETED.value
        poem.completed_at = now
        poem.next_execution_at = None
        poem.updated_at = now
        session.flush()

        StateManager.transition(
            session,
            AgentStatus.COMPLETED,
            poem_id=poem.id,
            reason=reason,
            last_execution_at=now,
            next_execution_at=None,
            update_next_execution=True,
            last_intervention_number=poem.completed_interventions,
            clear_active_poem=True,
        )

        self.log_event(
            session,
            poem_id=poem.id,
            event_type="POEM_COMPLETED",
            description=(
                f"Poema '{poem.title}' finalizado y archivado con "
                f"{poem.completed_interventions}/{poem.total_interventions} intervenciones. {reason}"
            ),
            status="COMPLETED",
            scheduled_at=scheduled_close,
            actual_execution_at=now,
            finished_at=now,
            details={
                "poem_id": str(poem.id),
                "title": poem.title,
                "completed_interventions": poem.completed_interventions,
                "total_interventions": poem.total_interventions,
                "started_at_utc": to_utc_iso(poem.started_at),
                "started_at_bogota": to_bogota_iso(poem.started_at),
                "completed_at_utc": to_utc_iso(now),
                "completed_at_bogota": to_bogota_iso(now),
            },
        )
        return poem

    def log_event(
        self,
        session: Session,
        *,
        poem_id: uuid.UUID | None,
        event_type: str,
        description: str,
        status: str,
        scheduled_at: datetime | None = None,
        actual_execution_at: datetime | None = None,
        finished_at: datetime | None = None,
        details: dict[str, Any] | None = None,
    ) -> AgentLog:
        """
        Registra un evento en `agent_logs` distinguiendo explícitamente entre:
        - Hora de creación del evento (`event_created_at`)
        - Hora programada (`scheduled_at`)
        - Hora real de ejecución (`actual_execution_at`)
        - Hora de finalización (`finished_at`)
        """
        now = utc_now()
        sched = ensure_utc(scheduled_at) if scheduled_at else now
        actual = ensure_utc(actual_execution_at) if actual_execution_at else now
        finish = ensure_utc(finished_at) if finished_at else now

        trace_details: dict[str, Any] = {
            "event_created_at_utc": to_utc_iso(now),
            "event_created_at_bogota": to_bogota_iso(now),
            "scheduled_at_utc": to_utc_iso(sched),
            "scheduled_at_bogota": to_bogota_iso(sched),
            "actual_execution_at_utc": to_utc_iso(actual),
            "actual_execution_at_bogota": to_bogota_iso(actual),
            "finished_at_utc": to_utc_iso(finish),
            "finished_at_bogota": to_bogota_iso(finish),
        }
        if details:
            trace_details.update(details)

        entry = AgentLog(
            id=uuid.uuid4(),
            poem_id=poem_id,
            event_type=event_type,
            description=description,
            status=status,
            details=trace_details,
            created_at=now,
        )
        session.add(entry)
        session.flush()
        return entry

    def get_poem_by_id(self, session: Session, poem_id: uuid.UUID) -> Poem:
        stmt = select(Poem).where(Poem.id == poem_id)
        poem = session.execute(stmt).scalar_one_or_none()
        if poem is None:
            raise PoemNotFoundError(f"No existe ningún poema con el identificador {poem_id}.")
        return poem

    def get_current_or_latest_poem(self, session: Session) -> Poem | None:
        """Retorna el poema activo; si no hay uno activo, retorna el último poema finalizado."""
        active = self.get_active_poem(session)
        if active is not None:
            return active
        stmt = select(Poem).order_by(desc(Poem.created_at)).limit(1)
        return session.execute(stmt).scalar_one_or_none()

    def get_poem_versions(self, session: Session, poem_id: uuid.UUID) -> list[PoemVersion]:
        """Retorna todas las versiones confirmadas de una composición en orden cronológico."""
        _ = self.get_poem_by_id(session, poem_id)
        stmt = (
            select(PoemVersion)
            .where(PoemVersion.poem_id == poem_id)
            .order_by(PoemVersion.intervention_number.asc())
        )
        return list(session.execute(stmt).scalars().all())

    def get_completed_poems_paginated(
        self,
        session: Session,
        *,
        page: int = 1,
        per_page: int = 10,
    ) -> tuple[list[Poem], int]:
        """Retorna los poemas finalizados (`status = COMPLETED`) con paginación."""
        page = max(1, page)
        per_page = max(1, min(50, per_page))

        count_stmt = select(func.count(Poem.id)).where(Poem.status == AgentStatus.COMPLETED.value)
        total = int(session.execute(count_stmt).scalar_one() or 0)

        stmt = (
            select(Poem)
            .where(Poem.status == AgentStatus.COMPLETED.value)
            .order_by(desc(Poem.completed_at), desc(Poem.created_at))
            .offset((page - 1) * per_page)
            .limit(per_page)
        )
        items = list(session.execute(stmt).scalars().all())
        return items, total

    def get_logs_paginated(
        self,
        session: Session,
        *,
        page: int = 1,
        per_page: int = 25,
        poem_id: uuid.UUID | None = None,
        event_type: str | None = None,
    ) -> tuple[list[AgentLog], int]:
        """Retorna el historial de actividades ordenado cronológicamente (más reciente primero)."""
        page = max(1, page)
        per_page = max(1, min(100, per_page))

        filters = []
        if poem_id is not None:
            filters.append(AgentLog.poem_id == poem_id)
        if event_type and event_type.strip():
            filters.append(AgentLog.event_type == event_type.strip().upper())

        count_stmt = select(func.count(AgentLog.id))
        if filters:
            count_stmt = count_stmt.where(*filters)
        total = int(session.execute(count_stmt).scalar_one() or 0)

        stmt = select(AgentLog)
        if filters:
            stmt = stmt.where(*filters)
        stmt = (
            stmt.order_by(desc(AgentLog.created_at))
            .offset((page - 1) * per_page)
            .limit(per_page)
        )
        items = list(session.execute(stmt).scalars().all())
        return items, total
