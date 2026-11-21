"""
Punto de Entrada del Proceso Autónomo Background Worker de POETA (worker.py).

Responsabilidades (Sección 6, Sección 11 y Sección 17):
- Ejecutar el agente autónomo en un proceso completamente independiente del servidor web Flask.
- Verificar la estructura de tablas en Supabase al iniciar.
- Recuperar ciclos activos tras reinicios de Render (`recover_on_startup`).
- Ejecutar las intervenciones programadas cada 10 minutos mediante APScheduler.
- Finalizar y archivar automáticamente el poema al cumplirse los 60 minutos.
- Manejar señales SIGINT / SIGTERM para un apagado limpio en contenedores Render.
"""

from __future__ import annotations

import logging
import signal
import sys
import threading
from types import FrameType

from agent.core import PoetaAgent
from agent.scheduler import PoetaScheduler
from config import load_config
from database.connection import DatabaseManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("poeta.worker")


def run_worker() -> None:
    """Inicia el Background Worker persistente de POETA y espera señal de término."""
    config = load_config()
    logger.info(
        "Iniciando POETA Background Worker (entorno=%s, modelo=%s, intervalo=%ds, ciclo=%ds)...",
        config.app_env,
        config.ollama_model,
        config.intervention_interval_seconds,
        config.cycle_duration_seconds,
    )

    db_manager = DatabaseManager(config)
    if config.has_database_configured or config.is_testing:
        try:
            db_manager.create_tables()
            logger.info("Esquema de base de datos verificado correctamente.")
        except Exception as exc:
            logger.error("No fue posible verificar las tablas al arranque del worker: %s", exc)
    else:
        logger.warning(
            "DATABASE_URL no está configurada. Configure la cadena de conexión de Supabase."
        )

    agent = PoetaAgent(config=config, db_manager=db_manager)
    scheduler = PoetaScheduler(agent=agent, config=config)

    stop_event = threading.Event()

    def _handle_shutdown(signum: int, _frame: FrameType | None) -> None:
        logger.info("Señal %s recibida. Deteniendo POETA Background Worker limpiamente...", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, _handle_shutdown)
    signal.signal(signal.SIGTERM, _handle_shutdown)

    outcome = scheduler.start(run_recovery=True)
    if outcome is not None:
        logger.info(
            "Resultado de recuperación inicial: acción=%s, poema=%s, intervenciones=%d, estado=%s",
            outcome.action_taken,
            outcome.poem_id,
            outcome.completed_interventions,
            outcome.agent_status,
        )

    logger.info("POETA Background Worker en ejecución autónoma. Esperando eventos programados...")
    try:
        while not stop_event.is_set():
            stop_event.wait(timeout=30.0)
    finally:
        scheduler.shutdown(wait=True)
        logger.info("POETA Background Worker detenido correctamente.")


if __name__ == "__main__":
    try:
        run_worker()
    except KeyboardInterrupt:
        sys.exit(0)
