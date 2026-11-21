"""
Pruebas Integrales del Ciclo de 60 Minutos del Agente POETA (tests/test_agent.py).

Verifica:
- Las 6 intervenciones distribuidas en los minutos 00, 10, 20, 30, 40 y 50.
- Cierre y archivado automático al minuto 60 sin generar una séptima estrofa.
- No inicio automático de otro poema después de finalizar.
- Bloqueo de inicio de un segundo ciclo mientras hay uno activo.
- Escenario A: Cuando Ollama Cloud falla en una intervención intermedia, se preserva
  la última versión válida y no se incrementa el contador de intervenciones.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agent.core import PoetaAgent
from agent.memory import ActiveCycleAlreadyExistsError
from agent.state_manager import AgentStatus
from tests.conftest import DeterministicLiteraryMockProvider


def test_full_60_minute_cycle_six_interventions_and_auto_archive(
    poeta_agent: PoetaAgent,
    mock_llm: DeterministicLiteraryMockProvider,
) -> None:
    t0 = datetime(2026, 11, 21, 12, 0, 0, tzinfo=timezone.utc)

    # Minuto 00: Intervención 1 (Título, temática y primera estrofa)
    started_poem = poeta_agent.start_cycle(seed_theme="El reloj de arena", current_time=t0)
    poem_id = started_poem["id"]
    assert started_poem["completed_interventions"] == 1
    assert started_poem["total_interventions"] == 6
    assert started_poem["status"] == AgentStatus.RUNNING.value

    # Intentar iniciar otro ciclo simultáneamente debe fallar
    with pytest.raises(ActiveCycleAlreadyExistsError):
        poeta_agent.start_cycle(current_time=t0 + timedelta(minutes=2))

    # Minutos 10, 20, 30, 40 y 50: Intervenciones 2 a 6
    for idx, minute_mark in enumerate([10, 20, 30, 40, 50], start=2):
        current_dt = t0 + timedelta(minutes=minute_mark)
        tick_res = poeta_agent.process_scheduled_tick(current_time=current_dt)
        assert tick_res["action"] == "INTERVENTION_PROCESSED"
        assert tick_res["executed"] is True
        assert tick_res["intervention_number"] == idx
        assert tick_res["completed_interventions"] == idx

    # Verificar que en el minuto 55 no se genera una séptima estrofa
    t55 = t0 + timedelta(minutes=55)
    tick_55 = poeta_agent.process_scheduled_tick(current_time=t55)
    assert tick_55["action"] == "WAITING_CLOSE"
    assert mock_llm.call_count == 6

    # Minuto 60: Cierre automático y archivado del poema (sin generar 7ª estrofa)
    t60 = t0 + timedelta(minutes=60)
    tick_60 = poeta_agent.process_scheduled_tick(current_time=t60)
    assert tick_60["action"] == "COMPLETED"
    assert mock_llm.call_count == 6

    # Verificar que el poema quedó en estado COMPLETED y con 6 versiones en la base de datos
    status = poeta_agent.get_agent_status_payload(current_time=t60)
    assert status["agent_status"] == AgentStatus.COMPLETED.value
    assert status["is_cycle_active"] is False
    assert status["active_poem"] is None
    assert status["latest_poem"]["id"] == poem_id
    assert status["latest_poem"]["completed_interventions"] == 6
    assert len(status["latest_poem"]["stanzas"]) == 6

    # Minuto 70: El agente NO inicia otro poema automáticamente
    t70 = t0 + timedelta(minutes=70)
    tick_70 = poeta_agent.process_scheduled_tick(current_time=t70)
    assert tick_70["action"] == "IDLE"
    assert mock_llm.call_count == 6


def test_scenario_a_ollama_failure_preserves_last_valid_version(
    poeta_agent: PoetaAgent,
    mock_llm: DeterministicLiteraryMockProvider,
) -> None:
    t0 = datetime(2026, 11, 21, 14, 0, 0, tzinfo=timezone.utc)
    poem = poeta_agent.start_cycle(current_time=t0)
    initial_text = poem["full_text"]
    assert poem["completed_interventions"] == 1

    # Minuto 10: Simular caída de Ollama Cloud
    mock_llm.should_fail = True
    t10 = t0 + timedelta(minutes=10)
    res_fail = poeta_agent.process_scheduled_tick(current_time=t10)
    assert res_fail["executed"] is False
    assert res_fail["error"] is True
    assert res_fail["completed_interventions"] == 1
    assert res_fail["agent_status"] == AgentStatus.ERROR.value

    # Verificar que la versión 1 sigue intacta en la base de datos
    status_after_fail = poeta_agent.get_agent_status_payload(current_time=t10)
    assert status_after_fail["active_poem"]["completed_interventions"] == 1
    assert status_after_fail["active_poem"]["full_text"] == initial_text

    # Cuando Ollama Cloud vuelve a responder, el reintento genera la intervención 2 sin duplicar la 1
    mock_llm.should_fail = False
    t12 = t0 + timedelta(minutes=12)
    res_retry = poeta_agent.process_scheduled_tick(current_time=t12)
    assert res_retry["executed"] is True
    assert res_retry["intervention_number"] == 2
    assert res_retry["completed_interventions"] == 2
