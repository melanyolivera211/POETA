"""
Sistema de Planificación Persistente de POETA (agent/scheduler.py).

Implementa la planificación autónoma mediante APScheduler (`BackgroundScheduler`),
diseñada para ejecutarse en el Background Worker independiente (`worker.py`).

Características:
- Recupera automáticamente cualquier ciclo activo en Supabase al iniciar (`recover_on_startup`).
- Programa y despacha las intervenciones cada 10 minutos y el cierre exacto al minuto 60.
- Controla ejecuciones simultáneas con `max_instances=1` y `coalesce=True`.
- Registra cualquier excepción inesperada en `agent_logs` sin detener el worker.
- Nunca utiliza `time.sleep` como mecanismo principal de planificación ni depende del navegador.
"""

from __future__ import annotations

from datetime import datetime, timezone
import logging
import threading
from typing import Any

from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED, JobExecutionEvent
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from agent.core import PoetaAgent
from agent.recovery import RecoveryOutcome
from config import AppConfig, ensure_utc, load_config, utc_now

logger = logging.getLogger("poeta.scheduler")


class PoetaScheduler:
    """Planificador persistente basado en APScheduler para el proceso autónomo POETA."""

    TICK_JOB_ID = "poeta_cycle_supervisor_tick"
    INTERVENTION_JOB_ID = "poeta_next_intervention_trigger"
    CLOSE_JOB_ID = "poeta_cycle_close_trigger"

    def __init__(
        self,
        agent: PoetaAgent,
        config: AppConfig | None = None,
    ) -> None:
        self.agent = agent
        self.config = config or agent.config or load_config()
        self._lock = threading.RLock()
        self.scheduler = BackgroundScheduler(
            timezone=timezone.utc,
            job_defaults={
                "coalesce": True,
                "max_instances": 1,
                "misfire_grace_time": 60,
            },
        )
        self.scheduler.add_listener(
            self._on_job_event,
            EVENT_JOB_EXECUTED | EVENT_JOB_ERROR,
        )
        self._started = False

    def start(self, *, run_recovery: bool = True) -> RecoveryOutcome | None:
        """
        Inicia el planificador APScheduler:
        1. Registra en la base de datos el evento de inicio del worker autónomo.
        2. Ejecuta la recuperación de estado (`recover_on_startup`).
        3. Configura el job supervisor y las tareas programadas del ciclo activo.
        """
        with self._lock:
            outcome: RecoveryOutcome | None = None
            if run_recovery:
                try:
                    with self.agent.db_manager.session_scope() as session:
                        self.agent.memory_repo.log_event(
                            session,
                            poem_id=None,
                            event_type="AGENT_STARTED",
                            description="Proceso autónomo Background Worker iniciado y conectado al planificador APScheduler.",
                            status="SUCCESS",
                        )
                    outcome = self.agent.recover_on_startup()
                except Exception as exc:
                    logger.error("Error durante la recuperación inicial del worker: %s", exc)

            poll_seconds = max(1, int(self.config.worker_poll_seconds))
            if self.config.accelerated_test_mode:
                poll_seconds = max(1, min(poll_seconds, self.config.intervention_interval_seconds // 2 or 1))

            self.scheduler.add_job(
                func=self.run_supervisor_cycle,
                trigger=IntervalTrigger(seconds=poll_seconds, timezone=timezone.utc),
                id=self.TICK_JOB_ID,
                name="Supervisor de ciclo poético POETA",
                replace_existing=True,
            )

            if not self._started:
                self.scheduler.start()
                self._started = True

            self.sync_jobs_from_database()
            return outcome

    def shutdown(self, wait: bool = True) -> None:
        """Detiene limpiamente el planificador APScheduler."""
        with self._lock:
            if self._started and self.scheduler.running:
                self.scheduler.shutdown(wait=wait)
            self._started = False

    def sync_jobs_from_database(self) -> dict[str, Any]:
        """
        Sincroniza los triggers exactos (`DateTrigger`) en APScheduler a partir del
        poema activo almacenado en Supabase (`next_execution_at` y `scheduled_end_at`).
        Si el ciclo ya finalizó, cancela las tareas programadas asociadas al ciclo.
        """
        with self._lock:
            try:
                with self.agent.db_manager.session_scope() as session:
                    active_poem = self.agent.memory_repo.get_active_poem(session)
                    if active_poem is None:
                        self._cancel_cycle_specific_jobs()
                        return {"synced": True, "active_poem": False}

                    next_exec = ensure_utc(active_poem.next_execution_at)
                    scheduled_end = ensure_utc(active_poem.scheduled_end_at)
                    poem_id_str = str(active_poem.id)
                    completed = active_poem.completed_interventions
                    total = active_poem.total_interventions
            except Exception as exc:
                logger.warning("No se pudo sincronizar tareas desde la base de datos: %s", exc)
                return {"synced": False, "error": str(exc)}

            now = utc_now()

            # Programar trigger exacto para la siguiente intervención si aún faltan estrofas
            if next_exec is not None and completed < total:
                run_date = max(now, next_exec)
                self.scheduler.add_job(
                    func=self.run_supervisor_cycle,
                    trigger=DateTrigger(run_date=run_date, timezone=timezone.utc),
                    id=self.INTERVENTION_JOB_ID,
                    name=f"Intervención #{completed + 1} del poema {poem_id_str}",
                    replace_existing=True,
                )
            else:
                self._remove_job_safely(self.INTERVENTION_JOB_ID)

            # Programar trigger exacto para el cierre automático al minuto 60
            if scheduled_end is not None:
                close_date = max(now, scheduled_end)
                self.scheduler.add_job(
                    func=self.run_supervisor_cycle,
                    trigger=DateTrigger(run_date=close_date, timezone=timezone.utc),
                    id=self.CLOSE_JOB_ID,
                    name=f"Cierre automático minuto 60 del poema {poem_id_str}",
                    replace_existing=True,
                )

            return {
                "synced": True,
                "active_poem": True,
                "poem_id": poem_id_str,
                "next_execution_at": next_exec,
                "scheduled_end_at": scheduled_end,
            }

    def run_supervisor_cycle(self) -> dict[str, Any]:
        """
        Ejecuta un ciclo de evaluación en el agente y actualiza los triggers programados.
        Protegido por un cerrojo reentrante (`_lock`) para evitar ejecuciones simultáneas.
        """
        with self._lock:
            try:
                result = self.agent.process_scheduled_tick()
                self.sync_jobs_from_database()
                return result
            except Exception as exc:
                logger.exception("Error durante la ejecución programada del agente: %s", exc)
                try:
                    with self.agent.db_manager.session_scope() as session:
                        self.agent.memory_repo.log_event(
                            session,
                            poem_id=None,
                            event_type="SCHEDULER_ERROR",
                            description=f"Error capturado en el planificador APScheduler: {exc}",
                            status="ERROR",
                            details={"error_type": type(exc).__name__, "error": str(exc)},
                        )
                except Exception:
                    pass
                return {"action": "ERROR", "error": str(exc)}

    def _cancel_cycle_specific_jobs(self) -> None:
        """Cancela las tareas específicas de un ciclo cuando éste finaliza."""
        self._remove_job_safely(self.INTERVENTION_JOB_ID)
        self._remove_job_safely(self.CLOSE_JOB_ID)

    def _remove_job_safely(self, job_id: str) -> None:
        if self.scheduler.get_job(job_id) is not None:
            try:
                self.scheduler.remove_job(job_id)
            except Exception:
                pass

    def _on_job_event(self, event: JobExecutionEvent) -> None:
        if event.exception:
            logger.error("Fallo en tarea programada '%s': %s", event.job_id, event.exception)
        else:
            logger.debug("Tarea programada '%s' ejecutada correctamente.", event.job_id)
