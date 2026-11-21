"""
Núcleo del Agente Autónomo POETA (agent/core.py).

Coordina la máquina de estados, el repositorio de memoria persistente en Supabase,
el motor de creación literaria y el cliente de Ollama Cloud.

Cumple las reglas de negocio obligatorias (Sección 5 y Sección 7):
- Ciclo de 60 minutos con 6 intervenciones cada 10 minutos (Minutos 00, 10, 20, 30, 40, 50).
- Al minuto 60 finaliza y archiva automáticamente sin generar una séptima estrofa.
- Exclusividad de 1 poema activo simultáneamente.
- Inicio no automático del siguiente poema tras completar un ciclo.
- Recuperación real de la memoria desde PostgreSQL antes de cada intervención.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
from typing import Any
import uuid

from api.ollama_client import LLMProvider, LLMProviderError, OllamaCloudClient
from config import (
    AppConfig,
    ensure_utc,
    format_bogota_display,
    load_config,
    to_bogota_iso,
    to_utc_iso,
    utc_now,
)
from database.connection import DatabaseManager
from database.models import Poem
from agent.memory import (
    ActiveCycleAlreadyExistsError,
    DuplicateInterventionError,
    PersistentMemoryRepository,
    PersistentPoemMemory,
)
from agent.poetry_engine import (
    DefectivePoemResponseError,
    InitialPoemDraft,
    PoetryEngine,
    StanzaContinuationDraft,
)
from agent.recovery import RecoveryManager, RecoveryOutcome
from agent.state_manager import AgentStatus, StateManager

logger = logging.getLogger("poeta.agent")


class PoetaAgent:
    """Agente inteligente persistente de creación literaria autónoma."""

    def __init__(
        self,
        config: AppConfig | None = None,
        db_manager: DatabaseManager | None = None,
        llm_provider: LLMProvider | None = None,
    ) -> None:
        self.config = config or load_config()
        self.db_manager = db_manager or DatabaseManager(self.config)
        self.llm_provider = llm_provider or OllamaCloudClient(self.config)
        self.memory_repo = PersistentMemoryRepository(self.db_manager)
        self.poetry_engine = PoetryEngine(self.llm_provider)
        self.recovery_manager = RecoveryManager(self.config, self.memory_repo)

    # ==========================================================================
    # 1. INICIO DEL CICLO (MINUTO 00 — INTERVENCIÓN 1 DE 6)
    # ==========================================================================
    def start_cycle(
        self,
        *,
        seed_theme: str | None = None,
        current_time: datetime | None = None,
    ) -> dict[str, Any]:
        """
        Inicia un nuevo ciclo de creación literaria de 60 minutos (Sección 7):
        1. Verifica que no exista otro poema activo (en código y en PostgreSQL).
        2. Crea el registro del nuevo ciclo.
        3. Registra fecha y hora de inicio (Minuto 00).
        4. Calcula fecha y hora de finalización (Minuto 60).
        5. Establece la próxima intervención (Minuto 10).
        6. Solicita la generación inicial al modelo (título, temática y estrofa 1).
        7. Valida la respuesta.
        8. Guarda el poema y su primera versión en Supabase.
        9. Registra el resultado de la operación.
        10. Actualiza el estado del agente a RUNNING.
        """
        started_at = ensure_utc(current_time) or utc_now()
        cycle_duration = timedelta(seconds=self.config.cycle_duration_seconds)
        interval_duration = timedelta(seconds=self.config.intervention_interval_seconds)

        scheduled_end_at = started_at + cycle_duration
        next_execution_at = started_at + interval_duration
        initial_prompt_desc = (
            f"Inspiración solicitada: {seed_theme.strip()}"
            if seed_theme and seed_theme.strip()
            else "Creación poética autónoma con temática libre en español."
        )

        # Paso 1-5: Verificar exclusividad y crear el registro del ciclo en PostgreSQL
        with self.db_manager.session_scope() as session:
            poem = self.memory_repo.initialize_cycle_record(
                session,
                initial_prompt=initial_prompt_desc,
                total_interventions=self.config.total_interventions,
                started_at=started_at,
                scheduled_end_at=scheduled_end_at,
                next_execution_at=next_execution_at,
            )
            poem_id = poem.id

            StateManager.transition(
                session,
                AgentStatus.GENERATING,
                poem_id=poem_id,
                reason="Enviando solicitud de generación inicial (Intervención 1/6) a Ollama Cloud.",
            )
            self.memory_repo.log_event(
                session,
                poem_id=poem_id,
                event_type="OLLAMA_REQUEST_SENT",
                description="Solicitud enviada a Ollama Cloud para generar título, temática y primera estrofa.",
                status="GENERATING",
                scheduled_at=started_at,
                actual_execution_at=started_at,
                details={
                    "intervention_number": 1,
                    "model": self.config.ollama_model,
                    "seed_theme": seed_theme,
                },
            )

        # Paso 6-7: Solicitar generación inicial al modelo y validar estructura
        try:
            draft: InitialPoemDraft = self.poetry_engine.generate_initial_composition(
                seed_theme=seed_theme
            )
        except (LLMProviderError, DefectivePoemResponseError) as exc:
            # Si falla la generación inicial, abortar/limpiar el ciclo para no dejar
            # un poema vacío bloqueando permanentemente al usuario, registrando el error.
            with self.db_manager.session_scope() as session:
                failed_poem = self.memory_repo.get_poem_by_id(session, poem_id)
                self.memory_repo.log_event(
                    session,
                    poem_id=poem_id,
                    event_type="ERROR_DETECTED",
                    description=f"Fallo en la generación inicial (Intervención 1): {exc}",
                    status="ERROR",
                    scheduled_at=started_at,
                    actual_execution_at=started_at,
                    details={"error_type": type(exc).__name__, "error": str(exc)},
                )
                StateManager.transition(
                    session,
                    AgentStatus.ERROR,
                    poem_id=poem_id,
                    reason=f"Fallo en generación inicial: {exc}",
                    clear_active_poem=True,
                    update_next_execution=True,
                    next_execution_at=None,
                )
                # Eliminar el poema no nacido para liberar el índice parcial de poema activo
                session.delete(failed_poem)
                StateManager.transition(
                    session,
                    AgentStatus.IDLE,
                    reason="Ciclo inicial revertido tras error del proveedor; sistema listo para reintentar.",
                    clear_active_poem=True,
                )
            raise

        # Paso 8-10: Guardar la primera versión, registrar logs y pasar a RUNNING
        with self.db_manager.session_scope() as session:
            poem = self.memory_repo.get_poem_by_id(session, poem_id)
            self.memory_repo.log_event(
                session,
                poem_id=poem.id,
                event_type="OLLAMA_RESPONSE_RECEIVED",
                description=(
                    f"Respuesta recibida de Ollama Cloud ({draft.model_name}) en {draft.duration_ms} ms. "
                    f"Título creado: '{draft.title}'."
                ),
                status="SUCCESS",
                scheduled_at=started_at,
                actual_execution_at=started_at,
                details={
                    "intervention_number": 1,
                    "title": draft.title,
                    "theme": draft.theme,
                    "duration_ms": draft.duration_ms,
                    "model_name": draft.model_name,
                },
            )

            self.memory_repo.save_confirmed_intervention(
                session,
                poem=poem,
                intervention_number=1,
                added_stanza=draft.first_stanza,
                full_text=draft.full_text,
                model_name=draft.model_name,
                prompt_snapshot=draft.prompt_snapshot,
                generation_duration_ms=draft.duration_ms,
                next_execution_at=next_execution_at,
                title=draft.title,
                theme=draft.theme,
                scheduled_at=started_at,
                execution_started_at=started_at,
            )

            return self._serialize_poem_summary(poem)

    # ==========================================================================
    # 2. INTERVENCIONES POSTERIORES (MINUTOS 10, 20, 30, 40, 50 — ESTROFAS 2..6)
    # ==========================================================================
    def execute_next_intervention(
        self,
        *,
        poem_id: uuid.UUID | None = None,
        current_time: datetime | None = None,
        triggered_by_recovery: bool = False,
    ) -> dict[str, Any]:
        """
        Ejecuta la siguiente intervención programada del poema activo (Sección 7):
        1. Consulta el poema activo en Supabase con bloqueo transaccional.
        2. Recupera la última versión almacenada, temática y número de intervención.
        3. Verifica que no se haya superado el minuto 60 ni las 6 intervenciones.
        4. Construye el contexto completo con la memoria recuperada desde PostgreSQL.
        5. Realiza la petición a Ollama Cloud.
        6. Valida la respuesta (Escenario D: rechaza respuestas vacías o que sustituyan el poema).
        7. Incorpora únicamente la nueva estrofa y construye la versión completa actualizada.
        8. Guarda la nueva versión inmutable y registra la actividad.
        9. Calcula la siguiente ejecución (o la hora de cierre al minuto 60 tras la 6ª intervención).
        """
        now = ensure_utc(current_time) or utc_now()

        with self.db_manager.session_scope() as session:
            if poem_id is not None:
                poem = self.memory_repo.get_poem_by_id(session, poem_id)
            else:
                poem = self.memory_repo.get_active_poem(session, lock_for_update=True)

            if poem is None:
                return {
                    "executed": False,
                    "reason": "NO_ACTIVE_POEM",
                    "agent_status": AgentStatus.IDLE.value,
                }

            scheduled_end = ensure_utc(poem.scheduled_end_at) or now
            scheduled_slot = ensure_utc(poem.next_execution_at) or now

            # Escenario E: Si ya se cumplieron los 60 minutos, cerrar y archivar sin generar más
            if now >= scheduled_end:
                self.memory_repo.finalize_and_archive_poem(
                    session,
                    poem,
                    reason="Tiempo del ciclo (60 minutos) cumplido. Archivado automático sin generar estrofas adicionales.",
                )
                return {
                    "executed": False,
                    "closed": True,
                    "reason": "CYCLE_TIME_REACHED",
                    "poem_id": str(poem.id),
                    "completed_interventions": poem.completed_interventions,
                    "agent_status": AgentStatus.COMPLETED.value,
                    "next_execution_at": None,
                }

            # Recuperar memoria persistente real desde PostgreSQL
            memory: PersistentPoemMemory | None = self.memory_repo.recover_poem_memory(
                session,
                poem.id,
                record_recovery_log=True,
            )
            assert memory is not None

            if memory.completed_interventions >= memory.total_interventions:
                # Las 6 intervenciones ya fueron completadas; programar espera hasta el minuto 60
                poem.next_execution_at = scheduled_end
                poem.updated_at = now
                return {
                    "executed": False,
                    "reason": "ALL_INTERVENTIONS_ALREADY_COMPLETED",
                    "poem_id": str(poem.id),
                    "completed_interventions": memory.completed_interventions,
                    "agent_status": poem.status,
                    "next_execution_at": scheduled_end,
                }

            next_intervention_num = memory.completed_interventions + 1

            # Verificar idempotencia antes de llamar al modelo
            if self.memory_repo.has_confirmed_intervention(session, poem.id, next_intervention_num):
                return {
                    "executed": False,
                    "reason": "INTERVENTION_ALREADY_CONFIRMED",
                    "poem_id": str(poem.id),
                    "completed_interventions": memory.completed_interventions,
                    "agent_status": poem.status,
                    "next_execution_at": poem.next_execution_at,
                }

            poem.status = AgentStatus.GENERATING.value
            poem.updated_at = now
            StateManager.transition(
                session,
                AgentStatus.GENERATING,
                poem_id=poem.id,
                reason=(
                    f"Generando intervención {next_intervention_num}/{memory.total_interventions} "
                    f"con memoria recuperada desde Supabase."
                ),
            )
            self.memory_repo.log_event(
                session,
                poem_id=poem.id,
                event_type="OLLAMA_REQUEST_SENT",
                description=(
                    f"Solicitud enviada a Ollama Cloud para la intervención "
                    f"{next_intervention_num}/{memory.total_interventions} de '{memory.title}'."
                ),
                status="GENERATING",
                scheduled_at=scheduled_slot,
                actual_execution_at=now,
                details={
                    "intervention_number": next_intervention_num,
                    "previous_stanzas_count": len(memory.stanzas),
                    "triggered_by_recovery": triggered_by_recovery,
                },
            )
            target_poem_id = poem.id

        # Petición fuera del lock de base de datos para no bloquear lecturas del dashboard,
        # manteniendo el estado GENERATING en la base de datos.
        try:
            if next_intervention_num == 1:
                init_draft = self.poetry_engine.generate_initial_composition()
                continuation = StanzaContinuationDraft(
                    intervention_number=1,
                    added_stanza=init_draft.first_stanza,
                    full_text=init_draft.full_text,
                    model_name=init_draft.model_name,
                    prompt_snapshot=init_draft.prompt_snapshot,
                    duration_ms=init_draft.duration_ms,
                )
                generated_title: str | None = init_draft.title
                generated_theme: str | None = init_draft.theme
            else:
                continuation = self.poetry_engine.generate_continuation_stanza(
                    title=memory.title,
                    theme=memory.theme,
                    previous_full_text=memory.full_text,
                    previous_stanzas=memory.stanzas,
                    intervention_number=next_intervention_num,
                    total_interventions=memory.total_interventions,
                )
                generated_title = None
                generated_theme = None

        except (LLMProviderError, DefectivePoemResponseError) as exc:
            # Escenario A y Escenario D:
            # - Registrar el error
            # - Mantener intacta la última versión válida
            # - No incrementar el contador de intervenciones
            # - Programar reintento controlado sin superar los 60 minutos
            err_time = now
            with self.db_manager.session_scope() as session:
                poem = self.memory_repo.get_poem_by_id(session, target_poem_id)
                retry_delay = min(60, max(5, self.config.intervention_interval_seconds // 2))
                retry_at = min(
                    err_time + timedelta(seconds=retry_delay),
                    ensure_utc(poem.scheduled_end_at) or err_time,
                )
                poem.status = AgentStatus.ERROR.value
                poem.next_execution_at = retry_at
                poem.updated_at = err_time

                self.memory_repo.log_event(
                    session,
                    poem_id=poem.id,
                    event_type="ERROR_DETECTED",
                    description=(
                        f"Fallo durante la intervención #{next_intervention_num}: {exc}. "
                        f"Se conserva intacta la versión #{poem.completed_interventions}."
                    ),
                    status="ERROR",
                    scheduled_at=scheduled_slot,
                    actual_execution_at=now,
                    finished_at=err_time,
                    details={
                        "intervention_attempted": next_intervention_num,
                        "preserved_version": poem.completed_interventions,
                        "error_type": type(exc).__name__,
                        "error_message": str(exc),
                        "retry_scheduled_at_utc": to_utc_iso(retry_at),
                        "retry_scheduled_at_bogota": to_bogota_iso(retry_at),
                    },
                )
                StateManager.transition(
                    session,
                    AgentStatus.ERROR,
                    poem_id=poem.id,
                    reason=(
                        f"Error en intervención #{next_intervention_num}. "
                        f"Versión #{poem.completed_interventions} preservada."
                    ),
                    next_execution_at=retry_at,
                    update_next_execution=True,
                )
            return {
                "executed": False,
                "error": True,
                "error_type": type(exc).__name__,
                "error_message": str(exc),
                "poem_id": str(target_poem_id),
                "completed_interventions": memory.completed_interventions,
                "agent_status": AgentStatus.ERROR.value,
                "next_execution_at": retry_at,
            }

        # Paso 8-12: Guardar la nueva versión confirmada de manera idempotente
        with self.db_manager.session_scope() as session:
            poem = self.memory_repo.get_poem_by_id(session, target_poem_id)
            next_slot = self.recovery_manager.calculate_next_slot_after_recovery(
                now=now,
                started_at=poem.started_at,
                scheduled_end_at=poem.scheduled_end_at,
                completed_interventions=next_intervention_num,
                total_interventions=poem.total_interventions,
            )

            self.memory_repo.log_event(
                session,
                poem_id=poem.id,
                event_type="OLLAMA_RESPONSE_RECEIVED",
                description=(
                    f"Respuesta válida recibida de Ollama Cloud ({continuation.model_name}) "
                    f"para la estrofa #{next_intervention_num} en {continuation.duration_ms} ms."
                ),
                status="SUCCESS",
                scheduled_at=scheduled_slot,
                actual_execution_at=now,
                details={
                    "intervention_number": next_intervention_num,
                    "duration_ms": continuation.duration_ms,
                    "model_name": continuation.model_name,
                },
            )

            try:
                version = self.memory_repo.save_confirmed_intervention(
                    session,
                    poem=poem,
                    intervention_number=next_intervention_num,
                    added_stanza=continuation.added_stanza,
                    full_text=continuation.full_text,
                    model_name=continuation.model_name,
                    prompt_snapshot=continuation.prompt_snapshot,
                    generation_duration_ms=continuation.duration_ms,
                    next_execution_at=next_slot,
                    title=generated_title,
                    theme=generated_theme,
                    scheduled_at=scheduled_slot,
                    execution_started_at=now,
                )
            except DuplicateInterventionError:
                return {
                    "executed": False,
                    "reason": "DUPLICATE_PREVENTED",
                    "poem_id": str(poem.id),
                    "completed_interventions": poem.completed_interventions,
                    "agent_status": poem.status,
                    "next_execution_at": poem.next_execution_at,
                }

            if triggered_by_recovery:
                self.memory_repo.log_event(
                    session,
                    poem_id=poem.id,
                    event_type="RECOVERY_COMPLETED",
                    description=(
                        f"Intervención pendiente #{next_intervention_num} ejecutada con éxito "
                        f"durante la recuperación controlada."
                    ),
                    status="RUNNING",
                    scheduled_at=scheduled_slot,
                    actual_execution_at=now,
                )

            return {
                "executed": True,
                "poem_id": str(poem.id),
                "version_id": str(version.id),
                "intervention_number": next_intervention_num,
                "completed_interventions": poem.completed_interventions,
                "total_interventions": poem.total_interventions,
                "agent_status": poem.status,
                "next_execution_at": next_slot,
            }

    # ==========================================================================
    # 3. CIERRE Y CICLO DE RELOJ DEL WORKER (TICK)
    # ==========================================================================
    def process_scheduled_tick(self, *, current_time: datetime | None = None) -> dict[str, Any]:
        """
        Evalúa el estado del ciclo activo en Supabase:
        - Si no hay ciclo activo, no hace nada (no inicia poemas automáticamente).
        - Si el ciclo activo alcanzó o superó `scheduled_end_at` (Minuto 60), lo cierra
          y archiva (`status = COMPLETED`).
        - Si hay una intervención pendiente cuya fecha `next_execution_at <= now` y
          `completed_interventions < 6`, ejecuta dicha intervención.
        """
        now = ensure_utc(current_time) or utc_now()

        with self.db_manager.session_scope() as session:
            active_poem = self.memory_repo.get_active_poem(session, lock_for_update=True)
            if active_poem is None:
                return {"action": "IDLE", "active_poem": False}

            scheduled_end = ensure_utc(active_poem.scheduled_end_at) or now
            next_exec = ensure_utc(active_poem.next_execution_at)
            poem_id = active_poem.id
            completed = active_poem.completed_interventions
            total = active_poem.total_interventions

            if now >= scheduled_end:
                self.memory_repo.finalize_and_archive_poem(
                    session,
                    active_poem,
                    reason="Minuto 60 alcanzado: cierre y archivado automático del poema.",
                )
                return {
                    "action": "COMPLETED",
                    "poem_id": str(poem_id),
                    "completed_interventions": completed,
                }

            if completed >= total:
                return {
                    "action": "WAITING_CLOSE",
                    "poem_id": str(poem_id),
                    "completed_interventions": completed,
                    "scheduled_end_at_utc": to_utc_iso(scheduled_end),
                }

            if next_exec is not None and now < next_exec:
                return {
                    "action": "WAITING_NEXT_INTERVENTION",
                    "poem_id": str(poem_id),
                    "completed_interventions": completed,
                    "next_execution_at_utc": to_utc_iso(next_exec),
                }

        # Ejecutar la siguiente intervención correspondiente
        result = self.execute_next_intervention(poem_id=poem_id, current_time=now)
        return {"action": "INTERVENTION_PROCESSED", **result}

    def recover_on_startup(self, *, current_time: datetime | None = None) -> RecoveryOutcome:
        """Invoca el protocolo de recuperación tras el arranque del Background Worker."""
        return self.recovery_manager.recover_after_restart(self, current_time=current_time)

    # ==========================================================================
    # 4. CONSULTA DE ESTADO Y TELEMETRÍA
    # ==========================================================================
    def get_agent_status_payload(self, *, current_time: datetime | None = None) -> dict[str, Any]:
        """
        Construye el payload completo para `GET /api/agent/status` (Sección 12):
        - Estado del agente
        - Poema activo
        - Número de intervención
        - Duración total
        - Tiempo transcurrido
        - Tiempo restante
        - Próxima ejecución (UTC y America/Bogota)
        - Última ejecución (UTC y America/Bogota)
        """
        now = ensure_utc(current_time) or utc_now()

        with self.db_manager.session_scope() as session:
            state = StateManager.get_or_create_state(session)
            active_poem = self.memory_repo.get_active_poem(session)
            latest_poem = active_poem or self.memory_repo.get_current_or_latest_poem(session)

            total_duration_seconds = self.config.cycle_duration_seconds
            total_duration_minutes = self.config.cycle_duration_minutes

            if active_poem is not None:
                started_at = ensure_utc(active_poem.started_at) or now
                scheduled_end_at = ensure_utc(active_poem.scheduled_end_at) or (
                    started_at + timedelta(seconds=total_duration_seconds)
                )
                elapsed_seconds = max(
                    0,
                    min(total_duration_seconds, int((now - started_at).total_seconds())),
                )
                remaining_seconds = max(0, int((scheduled_end_at - now).total_seconds()))
                progress_percent = round(
                    min(100.0, (elapsed_seconds / max(1, total_duration_seconds)) * 100.0),
                    1,
                )
                current_status = active_poem.status
                intervention_number = active_poem.completed_interventions
                next_exec = ensure_utc(active_poem.next_execution_at)
                active_summary = self._serialize_poem_summary(active_poem)
            else:
                elapsed_seconds = (
                    total_duration_seconds
                    if (latest_poem and latest_poem.status == AgentStatus.COMPLETED.value)
                    else 0
                )
                remaining_seconds = 0
                progress_percent = (
                    100.0
                    if (latest_poem and latest_poem.status == AgentStatus.COMPLETED.value)
                    else 0.0
                )
                current_status = state.agent_status
                intervention_number = (
                    latest_poem.completed_interventions
                    if latest_poem is not None
                    else state.last_intervention_number
                )
                next_exec = None
                active_summary = None

            last_exec = ensure_utc(state.last_execution_at)

            return {
                "agent_name": "POETA",
                "agent_status": current_status,
                "is_cycle_active": active_poem is not None,
                "active_poem": active_summary,
                "latest_poem": (
                    self._serialize_poem_summary(latest_poem) if latest_poem is not None else None
                ),
                "intervention_number": intervention_number,
                "total_interventions": self.config.total_interventions,
                "cycle_duration_minutes": total_duration_minutes,
                "cycle_duration_seconds": total_duration_seconds,
                "intervention_interval_minutes": self.config.intervention_interval_minutes,
                "intervention_interval_seconds": self.config.intervention_interval_seconds,
                "elapsed_seconds": elapsed_seconds,
                "elapsed_minutes": round(elapsed_seconds / 60.0, 2),
                "remaining_seconds": remaining_seconds,
                "remaining_minutes": round(remaining_seconds / 60.0, 2),
                "progress_percent": progress_percent,
                "next_execution_at_utc": to_utc_iso(next_exec),
                "next_execution_at_bogota": to_bogota_iso(next_exec),
                "next_execution_display": format_bogota_display(next_exec),
                "last_execution_at_utc": to_utc_iso(last_exec),
                "last_execution_at_bogota": to_bogota_iso(last_exec),
                "last_execution_display": format_bogota_display(last_exec),
                "display_timezone": self.config.display_timezone,
                "admin_auth_required": self.config.admin_auth_required,
                "server_time_utc": to_utc_iso(now),
                "server_time_bogota": to_bogota_iso(now),
                "server_time_display": format_bogota_display(now),
            }

    @staticmethod
    def _serialize_poem_summary(poem: Poem) -> dict[str, Any]:
        stanzas = [s.strip() for s in (poem.full_text or "").split("\n\n") if s.strip()]
        return {
            "id": str(poem.id),
            "title": poem.title,
            "theme": poem.theme,
            "initial_prompt": poem.initial_prompt,
            "status": poem.status,
            "full_text": poem.full_text,
            "stanzas": stanzas,
            "total_interventions": poem.total_interventions,
            "completed_interventions": poem.completed_interventions,
            "started_at_utc": to_utc_iso(poem.started_at),
            "started_at_bogota": to_bogota_iso(poem.started_at),
            "started_at_display": format_bogota_display(poem.started_at),
            "scheduled_end_at_utc": to_utc_iso(poem.scheduled_end_at),
            "scheduled_end_at_bogota": to_bogota_iso(poem.scheduled_end_at),
            "scheduled_end_at_display": format_bogota_display(poem.scheduled_end_at),
            "next_execution_at_utc": to_utc_iso(poem.next_execution_at),
            "next_execution_at_bogota": to_bogota_iso(poem.next_execution_at),
            "next_execution_at_display": format_bogota_display(poem.next_execution_at),
            "completed_at_utc": to_utc_iso(poem.completed_at),
            "completed_at_bogota": to_bogota_iso(poem.completed_at),
            "completed_at_display": format_bogota_display(poem.completed_at),
            "updated_at_utc": to_utc_iso(poem.updated_at),
            "updated_at_bogota": to_bogota_iso(poem.updated_at),
            "updated_at_display": format_bogota_display(poem.updated_at),
        }
