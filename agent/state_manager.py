"""
Máquina de Estados Controlada de POETA (agent/state_manager.py).

Estados obligatorios (Sección 7):
- IDLE: esperando el inicio.
- INITIALIZING: preparando el poema.
- RUNNING: ciclo en ejecución.
- GENERATING: realizando una petición al modelo.
- RECOVERING: recuperando información persistente.
- ERROR: error que requiere atención.
- COMPLETED: ciclo finalizado.

Todas las transiciones son explícitas, validadas y registradas en `agent_logs`
y `agent_state` dentro de PostgreSQL.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from config import ensure_utc, to_bogota_iso, to_utc_iso, utc_now
from database.models import AgentLog, AgentState, SINGLETON_AGENT_STATE_ID


class AgentStatus(str, Enum):
    """Estados oficiales de la máquina de estados del agente POETA."""

    IDLE = "IDLE"
    INITIALIZING = "INITIALIZING"
    RUNNING = "RUNNING"
    GENERATING = "GENERATING"
    RECOVERING = "RECOVERING"
    ERROR = "ERROR"
    COMPLETED = "COMPLETED"


# Mapa explícito de transiciones válidas entre estados
VALID_STATE_TRANSITIONS: dict[AgentStatus, set[AgentStatus]] = {
    AgentStatus.IDLE: {
        AgentStatus.INITIALIZING,
        AgentStatus.RECOVERING,
        AgentStatus.IDLE,
    },
    AgentStatus.INITIALIZING: {
        AgentStatus.GENERATING,
        AgentStatus.RUNNING,
        AgentStatus.ERROR,
        AgentStatus.IDLE,
    },
    AgentStatus.RUNNING: {
        AgentStatus.GENERATING,
        AgentStatus.RECOVERING,
        AgentStatus.COMPLETED,
        AgentStatus.ERROR,
        AgentStatus.RUNNING,
    },
    AgentStatus.GENERATING: {
        AgentStatus.RUNNING,
        AgentStatus.COMPLETED,
        AgentStatus.ERROR,
        AgentStatus.RECOVERING,
    },
    AgentStatus.RECOVERING: {
        AgentStatus.RUNNING,
        AgentStatus.GENERATING,
        AgentStatus.COMPLETED,
        AgentStatus.IDLE,
        AgentStatus.ERROR,
        AgentStatus.RECOVERING,
    },
    AgentStatus.ERROR: {
        AgentStatus.RECOVERING,
        AgentStatus.GENERATING,
        AgentStatus.RUNNING,
        AgentStatus.COMPLETED,
        AgentStatus.IDLE,
        AgentStatus.ERROR,
    },
    AgentStatus.COMPLETED: {
        AgentStatus.INITIALIZING,
        AgentStatus.IDLE,
        AgentStatus.RECOVERING,
        AgentStatus.COMPLETED,
    },
}


class InvalidStateTransitionError(RuntimeError):
    """Excepción lanzada cuando se intenta una transición de estado no permitida."""


class StateManager:
    """Gestiona y persiste las transiciones de la máquina de estados en PostgreSQL."""

    @staticmethod
    def get_or_create_state(session: Session, lock_for_update: bool = False) -> AgentState:
        """
        Obtiene la fila singleton `agent_state`. Si se solicita `lock_for_update=True`
        y el motor es PostgreSQL, aplica `SELECT ... FOR UPDATE` para control de concurrencia.
        """
        stmt = select(AgentState).where(AgentState.id == SINGLETON_AGENT_STATE_ID)
        if lock_for_update:
            stmt = stmt.with_for_update()

        state = session.execute(stmt).scalar_one_or_none()
        if state is None:
            now = utc_now()
            state = AgentState(
                id=SINGLETON_AGENT_STATE_ID,
                agent_status=AgentStatus.IDLE.value,
                active_poem_id=None,
                last_execution_at=None,
                next_execution_at=None,
                last_intervention_number=0,
                updated_at=now,
            )
            session.add(state)
            session.flush()
        return state

    @classmethod
    def transition(
        cls,
        session: Session,
        to_status: AgentStatus,
        *,
        poem_id: uuid.UUID | None = None,
        reason: str = "",
        last_execution_at: datetime | None = None,
        next_execution_at: datetime | None = None,
        update_next_execution: bool = False,
        last_intervention_number: int | None = None,
        clear_active_poem: bool = False,
        extra_details: dict[str, Any] | None = None,
    ) -> AgentState:
        """
        Ejecuta una transición explícita en la máquina de estados, actualiza `agent_state`
        y registra el evento en `agent_logs` con marcas temporales completas.
        """
        state = cls.get_or_create_state(session, lock_for_update=True)
        current_raw = state.agent_status or AgentStatus.IDLE.value
        try:
            from_status = AgentStatus(current_raw)
        except ValueError:
            from_status = AgentStatus.IDLE

        allowed = VALID_STATE_TRANSITIONS.get(from_status, set())
        if to_status not in allowed:
            raise InvalidStateTransitionError(
                f"Transición de estado inválida: {from_status.value} -> {to_status.value}"
            )

        now = utc_now()
        state.agent_status = to_status.value
        if clear_active_poem:
            state.active_poem_id = None
        elif poem_id is not None:
            state.active_poem_id = poem_id

        if last_execution_at is not None:
            state.last_execution_at = ensure_utc(last_execution_at)
        if update_next_execution:
            state.next_execution_at = ensure_utc(next_execution_at)
        if last_intervention_number is not None:
            state.last_intervention_number = last_intervention_number

        state.updated_at = now
        session.flush()

        log_poem_id = poem_id if poem_id is not None else state.active_poem_id
        details_payload: dict[str, Any] = {
            "from_state": from_status.value,
            "to_state": to_status.value,
            "event_created_at_utc": to_utc_iso(now),
            "event_created_at_bogota": to_bogota_iso(now),
            "scheduled_at_utc": to_utc_iso(state.next_execution_at),
            "scheduled_at_bogota": to_bogota_iso(state.next_execution_at),
            "actual_execution_at_utc": to_utc_iso(now),
            "actual_execution_at_bogota": to_bogota_iso(now),
        }
        if extra_details:
            details_payload.update(extra_details)

        description = (
            f"Transición de estado: {from_status.value} -> {to_status.value}."
            + (f" {reason}" if reason else "")
        )

        log_entry = AgentLog(
            poem_id=log_poem_id,
            event_type="STATE_TRANSITION",
            description=description.strip(),
            status=to_status.value,
            details=details_payload,
            created_at=now,
        )
        session.add(log_entry)
        session.flush()
        return state
