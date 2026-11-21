"""
Pruebas de Recuperación tras Reinicio del Worker (tests/test_recovery.py).

Verifica los requisitos de la Sección 8 y Escenarios B / E:
- Si el worker se reinicia con un ciclo activo sin tareas atrasadas, retoma la programación.
- Si el worker estuvo apagado durante varios intervalos (por ejemplo, del minuto 0 al 35),
  NO ejecuta todas las intervenciones atrasadas de golpe: ejecuta como máximo UNA
  intervención pendiente y recalcula la próxima fecha sin extender la duración de 60 minutos.
- Si el worker se reinicia cuando ya transcurrieron los 60 minutos, cierra y archiva
  el ciclo inmediatamente sin generar más contenido.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from agent.core import PoetaAgent
from agent.state_manager import AgentStatus
from tests.conftest import DeterministicLiteraryMockProvider


def test_recovery_after_multi_interval_downtime_executes_at_most_one_intervention(
    poeta_agent: PoetaAgent,
    mock_llm: DeterministicLiteraryMockProvider,
) -> None:
    t0 = datetime(2026, 11, 21, 16, 0, 0, tzinfo=timezone.utc)
    started = poeta_agent.start_cycle(current_time=t0)
    original_scheduled_end = started["scheduled_end_at_utc"]
    assert started["completed_interventions"] == 1
    assert mock_llm.call_count == 1

    # Simular que el worker estuvo apagado durante 35 minutos (se perdieron los minutos 10, 20 y 30)
    t35 = t0 + timedelta(minutes=35)
    outcome = poeta_agent.recover_on_startup(current_time=t35)

    assert outcome.recovered is True
    assert outcome.action_taken == "EXECUTED_SINGLE_PENDING_INTERVENTION"
    # Solo debe haberse ejecutado UNA intervención pendiente (pasando de 1 a 2), no 3 de golpe
    assert outcome.completed_interventions == 2
    assert mock_llm.call_count == 2

    # Verificar que la fecha de finalización de 60 minutos NO se extendió artificialmente
    status = poeta_agent.get_agent_status_payload(current_time=t35)
    assert status["active_poem"]["scheduled_end_at_utc"] == original_scheduled_end
    assert status["active_poem"]["completed_interventions"] == 2


def test_recovery_closes_and_archives_cycle_if_60_minutes_already_elapsed(
    poeta_agent: PoetaAgent,
    mock_llm: DeterministicLiteraryMockProvider,
) -> None:
    t0 = datetime(2026, 11, 21, 18, 0, 0, tzinfo=timezone.utc)
    _ = poeta_agent.start_cycle(current_time=t0)
    assert mock_llm.call_count == 1

    # Simular que el worker vuelve a encenderse a los 65 minutos
    t65 = t0 + timedelta(minutes=65)
    outcome = poeta_agent.recover_on_startup(current_time=t65)

    assert outcome.recovered is True
    assert outcome.action_taken == "CLOSED_EXPIRED_CYCLE"
    assert outcome.agent_status == AgentStatus.COMPLETED.value
    # No debe haber generado ninguna estrofa adicional tras los 60 minutos
    assert mock_llm.call_count == 1

    status = poeta_agent.get_agent_status_payload(current_time=t65)
    assert status["is_cycle_active"] is False
    assert status["agent_status"] == AgentStatus.COMPLETED.value
