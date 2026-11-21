"""
Pruebas del Planificador APScheduler y Modo Acelerado de Pruebas (tests/test_scheduler.py).

Verifica:
- Que el modo acelerado NUNCA pueda activarse cuando `APP_ENV=production` o `development`.
- Que el planificador APScheduler sincronice los triggers desde la base de datos,
  ejecute el ciclo y cancele los jobs al finalizar.
"""

from __future__ import annotations

from agent.core import PoetaAgent
from agent.scheduler import PoetaScheduler
from config import AppConfig, load_config
from database.connection import DatabaseManager
from tests.conftest import DeterministicLiteraryMockProvider


def test_accelerated_mode_is_strictly_blocked_in_production() -> None:
    prod_cfg = load_config(
        overrides={
            "APP_ENV": "production",
            "POETA_ACCELERATED_TEST": "true",
            "TEST_CYCLE_DURATION_SECONDS": "12",
            "TEST_INTERVENTION_INTERVAL_SECONDS": "2",
        }
    )
    assert prod_cfg.is_production is True
    assert prod_cfg.accelerated_test_mode is False
    assert prod_cfg.cycle_duration_seconds == 3600
    assert prod_cfg.intervention_interval_seconds == 600


def test_apscheduler_lifecycle_and_database_job_sync(
    accelerated_config: AppConfig,
) -> None:
    assert accelerated_config.accelerated_test_mode is True
    assert accelerated_config.cycle_duration_seconds == 6
    assert accelerated_config.intervention_interval_seconds == 1

    db_manager = DatabaseManager(accelerated_config)
    db_manager.create_tables()
    mock_llm = DeterministicLiteraryMockProvider()
    agent = PoetaAgent(
        config=accelerated_config,
        db_manager=db_manager,
        llm_provider=mock_llm,
    )
    scheduler = PoetaScheduler(agent=agent, config=accelerated_config)

    try:
        outcome = scheduler.start(run_recovery=True)
        assert outcome is not None
        assert outcome.action_taken == "NO_ACTIVE_CYCLE"
        assert scheduler.scheduler.running is True

        # Iniciar un ciclo y sincronizar jobs en APScheduler
        poem = agent.start_cycle(seed_theme="El pulso del tiempo")
        sync_info = scheduler.sync_jobs_from_database()
        assert sync_info["synced"] is True
        assert sync_info["active_poem"] is True
        assert sync_info["poem_id"] == poem["id"]
        assert scheduler.scheduler.get_job(PoetaScheduler.INTERVENTION_JOB_ID) is not None
        assert scheduler.scheduler.get_job(PoetaScheduler.CLOSE_JOB_ID) is not None

        # Verificar que al finalizar el ciclo las tareas específicas del ciclo se cancelen
        from datetime import timedelta
        from config import utc_now

        agent.process_scheduled_tick(current_time=utc_now() + timedelta(seconds=10))
        after_close_sync = scheduler.sync_jobs_from_database()
        assert after_close_sync["active_poem"] is False
        assert scheduler.scheduler.get_job(PoetaScheduler.INTERVENTION_JOB_ID) is None
        assert scheduler.scheduler.get_job(PoetaScheduler.CLOSE_JOB_ID) is None
    finally:
        scheduler.shutdown(wait=False)
