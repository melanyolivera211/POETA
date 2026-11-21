"""
Módulo de Recuperación de Estado y Manejo de Contingencias (agent/recovery.py).

Implementa los escenarios obligatorios de la Sección 8 y la Sección 14:
- Escenario A: Ollama Cloud no responde (mantiene última versión válida, no incrementa
  contador, registra error, programa reintento controlado).
- Escenario B: Render reinicia el Background Worker (recupera ciclo activo desde Supabase,
  sincroniza última intervención confirmada, ejecuta como máximo UNA intervención
  pendiente si hubo varios intervalos apagado y recalcula la próxima fecha sin extender
  los 60 minutos totales).
- Escenario C: Supabase no está disponible (no confirma intervención, reintenta con control).
- Escenario D: Respuesta defectuosa del modelo (rechaza contenido vacío o sustitución total,
  conserva intacta la versión anterior).
- Escenario E: El ciclo alcanza o supera los 60 minutos (cierra y archiva automáticamente
  sin generar una séptima estrofa ni contenido adicional).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
from typing import TYPE_CHECKING, Any

from config import AppConfig, ensure_utc, to_bogota_iso, to_utc_iso, utc_now
from database.models import Poem
from agent.memory import PersistentMemoryRepository, PersistentPoemMemory
from agent.state_manager import AgentStatus, StateManager

if TYPE_CHECKING:
    from agent.core import PoetaAgent

logger = logging.getLogger("poeta.recovery")


@dataclass(frozen=True)
class RecoveryOutcome:
    """Resultado estructurado del proceso de recuperación tras un reinicio."""

    recovered: bool
    action_taken: str
    poem_id: str | None
    completed_interventions: int
    agent_status: str
    next_execution_at: datetime | None
    details: dict[str, Any]


class RecoveryManager:
    """Coordinador de recuperación de estado persistente y resiliencia del agente."""

    def __init__(
        self,
        config: AppConfig,
        memory_repo: PersistentMemoryRepository,
    ) -> None:
        self.config = config
        self.memory_repo = memory_repo

    def calculate_next_slot_after_recovery(
        self,
        *,
        now: datetime,
        started_at: datetime,
        scheduled_end_at: datetime,
        completed_interventions: int,
        total_interventions: int,
    ) -> datetime:
        """
        Recalcula la siguiente fecha de ejecución después de una recuperación o intervención,
        sin extender artificialmente la duración total de 60 minutos (`scheduled_end_at`).
        """
        now_utc = ensure_utc(now) or utc_now()
        start_utc = ensure_utc(started_at) or now_utc
        end_utc = ensure_utc(scheduled_end_at) or (
            start_utc + timedelta(seconds=self.config.cycle_duration_seconds)
        )

        if completed_interventions >= total_interventions:
            return end_utc

        interval = timedelta(seconds=self.config.intervention_interval_seconds)
        nominal_slot = start_utc + (interval * completed_interventions)

        if nominal_slot > now_utc:
            return min(nominal_slot, end_utc)

        # Si el servicio estuvo apagado durante varios intervalos, la siguiente
        # ejecución se programa un intervalo adelante desde `now_utc`, pero siempre
        # acotada por `scheduled_end_at` para no extender jamás los 60 minutos.
        next_controlled = now_utc + interval
        return min(next_controlled, end_utc)

    def recover_after_restart(
        self,
        agent: "PoetaAgent",
        *,
        current_time: datetime | None = None,
    ) -> RecoveryOutcome:
        """
        Ejecuta el protocolo completo de recuperación cuando el Background Worker se inicia
        o se recupera de una interrupción (Sección 8 y Escenario B / E).
        """
        now = ensure_utc(current_time) or utc_now()

        with self.memory_repo.db.session_scope() as session:
            active_poem: Poem | None = self.memory_repo.get_active_poem(
                session,
                lock_for_update=True,
            )

            if active_poem is None:
                state = StateManager.get_or_create_state(session, lock_for_update=True)
                if state.agent_status not in (AgentStatus.IDLE.value, AgentStatus.COMPLETED.value):
                    StateManager.transition(
                        session,
                        AgentStatus.IDLE,
                        reason="Reinicio del worker verificado: no existen ciclos activos pendientes.",
                        clear_active_poem=True,
                        update_next_execution=True,
                        next_execution_at=None,
                    )
                return RecoveryOutcome(
                    recovered=False,
                    action_taken="NO_ACTIVE_CYCLE",
                    poem_id=None,
                    completed_interventions=0,
                    agent_status=state.agent_status,
                    next_execution_at=None,
                    details={"message": "No existe un ciclo activo para recuperar."},
                )

            # 1. Transicionar a RECOVERING y reconstruir memoria real desde poem_versions
            StateManager.transition(
                session,
                AgentStatus.RECOVERING,
                poem_id=active_poem.id,
                reason="Recuperando estado del ciclo activo tras reinicio del proceso autónomo.",
            )

            memory: PersistentPoemMemory | None = self.memory_repo.recover_poem_memory(
                session,
                active_poem.id,
                record_recovery_log=True,
            )
            assert memory is not None

            scheduled_end = ensure_utc(active_poem.scheduled_end_at) or (
                memory.started_at + timedelta(seconds=self.config.cycle_duration_seconds)
            )
            next_exec = ensure_utc(active_poem.next_execution_at)

            # 2. Escenario E: ¿El ciclo ya cumplió o superó los 60 minutos?
            if now >= scheduled_end:
                if memory.completed_interventions == 0:
                    # Si un ciclo quedó en INITIALIZING sin ninguna versión y ya expiró
                    active_poem.title = active_poem.title or "Ciclo expirado sin generación"
                self.memory_repo.finalize_and_archive_poem(
                    session,
                    active_poem,
                    reason=(
                        "Recuperación tras reinicio: el tiempo límite de 60 minutos ya se cumplió. "
                        "El ciclo se cierra y archiva sin generar intervenciones adicionales."
                    ),
                )
                self.memory_repo.log_event(
                    session,
                    poem_id=active_poem.id,
                    event_type="RECOVERY_COMPLETED",
                    description=(
                        "Ciclo finalizado durante la recuperación por haber alcanzado los 60 minutos."
                    ),
                    status="COMPLETED",
                    scheduled_at=scheduled_end,
                    actual_execution_at=now,
                    finished_at=now,
                )
                return RecoveryOutcome(
                    recovered=True,
                    action_taken="CLOSED_EXPIRED_CYCLE",
                    poem_id=str(active_poem.id),
                    completed_interventions=memory.completed_interventions,
                    agent_status=AgentStatus.COMPLETED.value,
                    next_execution_at=None,
                    details={
                        "scheduled_end_at_utc": to_utc_iso(scheduled_end),
                        "recovered_at_utc": to_utc_iso(now),
                    },
                )

            # 3. Si ya completó las 6 intervenciones pero aún no llega al minuto 60
            if memory.completed_interventions >= memory.total_interventions:
                active_poem.status = AgentStatus.RUNNING.value
                active_poem.next_execution_at = scheduled_end
                active_poem.updated_at = now
                StateManager.transition(
                    session,
                    AgentStatus.RUNNING,
                    poem_id=active_poem.id,
                    reason="Las 6 intervenciones están completas; esperando cierre al minuto 60.",
                    next_execution_at=scheduled_end,
                    update_next_execution=True,
                    last_intervention_number=memory.completed_interventions,
                )
                self.memory_repo.log_event(
                    session,
                    poem_id=active_poem.id,
                    event_type="RECOVERY_COMPLETED",
                    description=(
                        "Ciclo recuperado con 6/6 intervenciones. Cierre programado al minuto 60."
                    ),
                    status="RUNNING",
                    scheduled_at=scheduled_end,
                    actual_execution_at=now,
                )
                return RecoveryOutcome(
                    recovered=True,
                    action_taken="AWAITING_MINUTE_60_CLOSE",
                    poem_id=str(active_poem.id),
                    completed_interventions=memory.completed_interventions,
                    agent_status=AgentStatus.RUNNING.value,
                    next_execution_at=scheduled_end,
                    details={"scheduled_end_at_bogota": to_bogota_iso(scheduled_end)},
                )

            # 4. Determinar si corresponde ejecutar UNA intervención pendiente ahora mismo
            is_intervention_due = next_exec is None or now >= next_exec
            poem_id_to_process = active_poem.id

            if not is_intervention_due:
                active_poem.status = AgentStatus.RUNNING.value
                active_poem.updated_at = now
                StateManager.transition(
                    session,
                    AgentStatus.RUNNING,
                    poem_id=active_poem.id,
                    reason=(
                        f"Ciclo recuperado ({memory.completed_interventions}/{memory.total_interventions}). "
                        f"Próxima ejecución vigente: {to_bogota_iso(next_exec)}."
                    ),
                    next_execution_at=next_exec,
                    update_next_execution=True,
                    last_intervention_number=memory.completed_interventions,
                )
                self.memory_repo.log_event(
                    session,
                    poem_id=active_poem.id,
                    event_type="RECOVERY_COMPLETED",
                    description=(
                        f"Recuperación exitosa sin intervenciones atrasadas. "
                        f"Próxima intervención #{memory.completed_interventions + 1} programada."
                    ),
                    status="RUNNING",
                    scheduled_at=next_exec,
                    actual_execution_at=now,
                )
                return RecoveryOutcome(
                    recovered=True,
                    action_taken="RESUMED_SCHEDULE",
                    poem_id=str(active_poem.id),
                    completed_interventions=memory.completed_interventions,
                    agent_status=AgentStatus.RUNNING.value,
                    next_execution_at=next_exec,
                    details={"next_execution_at_bogota": to_bogota_iso(next_exec)},
                )

        # 5. Si había una o varias intervenciones atrasadas (el worker estuvo apagado),
        # ejecutamos COMO MÁXIMO UNA intervención pendiente y recalculamos la próxima fecha.
        step_result = agent.execute_next_intervention(
            poem_id=poem_id_to_process,
            current_time=now,
            triggered_by_recovery=True,
        )

        return RecoveryOutcome(
            recovered=True,
            action_taken="EXECUTED_SINGLE_PENDING_INTERVENTION",
            poem_id=str(poem_id_to_process),
            completed_interventions=step_result.get("completed_interventions", 0),
            agent_status=step_result.get("agent_status", AgentStatus.RUNNING.value),
            next_execution_at=step_result.get("next_execution_at"),
            details=step_result,
        )
