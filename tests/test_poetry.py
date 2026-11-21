"""
Pruebas del Motor de Creación Literaria y Cliente Google Gemini API (tests/test_poetry.py).
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest
import requests

from agent.poetry_engine import DefectivePoemResponseError, PoetryEngine
from api.ollama_client import (
    AIConfigurationError,
    AIConnectionError,
    AIResponseError,
    GeminiCloudClient,
    create_llm_provider,
    scrub_secrets,
)
from config import load_config
from tests.conftest import DeterministicLiteraryMockProvider


def test_initial_composition_generation_and_parsing(
    mock_llm: DeterministicLiteraryMockProvider,
) -> None:
    engine = PoetryEngine(mock_llm)
    draft = engine.generate_initial_composition(seed_theme="El paso del tiempo")

    assert draft.title == "Liturgia del Reloj de Arena"
    assert "memoria" in draft.theme.lower()
    assert len(draft.first_stanza.splitlines()) == 4
    assert draft.full_text == draft.first_stanza
    assert draft.duration_ms > 0
    assert "El paso del tiempo" in mock_llm.recorded_prompts[0]["user_prompt"]


def test_continuation_stanza_includes_recovered_memory_and_combines_text(
    mock_llm: DeterministicLiteraryMockProvider,
) -> None:
    engine = PoetryEngine(mock_llm)
    initial = engine.generate_initial_composition()

    cont = engine.generate_continuation_stanza(
        title=initial.title,
        theme=initial.theme,
        previous_full_text=initial.full_text,
        previous_stanzas=[initial.first_stanza],
        intervention_number=2,
        total_interventions=6,
    )

    assert cont.intervention_number == 2
    assert cont.added_stanza != initial.first_stanza
    assert cont.full_text == f"{initial.first_stanza}\n\n{cont.added_stanza}"

    sent_prompt = mock_llm.recorded_prompts[-1]["user_prompt"]
    assert "MEMORIA PERSISTENTE DEL POEMA" in sent_prompt
    assert initial.title in sent_prompt
    assert initial.theme in sent_prompt
    assert initial.first_stanza in sent_prompt


def test_scenario_d_rejects_empty_or_malformed_initial_response(
    mock_llm: DeterministicLiteraryMockProvider,
) -> None:
    engine = PoetryEngine(mock_llm)
    mock_llm.custom_next_response = "Un texto suelto sin estructura de titulo ni estrofa."

    with pytest.raises(DefectivePoemResponseError):
        engine.generate_initial_composition()


def test_scenario_d_rejects_response_that_replaces_entire_poem_or_has_multiple_stanzas(
    mock_llm: DeterministicLiteraryMockProvider,
) -> None:
    engine = PoetryEngine(mock_llm)
    prev_stanza = (
        "Cae la arena fina en el cristal dormido,\n"
        "como un río secreto que numera la sombra,\n"
        "y en cada grano leve que se juzga perdido\n"
        "late un eco antiguo que en silencio nos nombra."
    )

    mock_llm.custom_next_response = (
        f"{prev_stanza}\n\n"
        "Segunda estrofa añadida ilegalmente junto con la primera,\n"
        "intentando sustituir todo el poema acumulado."
    )
    with pytest.raises(DefectivePoemResponseError):
        engine.generate_continuation_stanza(
            title="Liturgia del Reloj de Arena",
            theme="La memoria",
            previous_full_text=prev_stanza,
            previous_stanzas=[prev_stanza],
            intervention_number=2,
        )

    mock_llm.custom_next_response = prev_stanza
    with pytest.raises(DefectivePoemResponseError):
        engine.generate_continuation_stanza(
            title="Liturgia del Reloj de Arena",
            theme="La memoria",
            previous_full_text=prev_stanza,
            previous_stanzas=[prev_stanza],
            intervention_number=2,
        )


def test_gemini_client_secret_scrubbing_and_unconfigured_guard() -> None:
    secret_key = "AQ.FakeTestKeyForScrubbingUnitTests0000000000000000"
    raw_error = f"Failed with key {secret_key} at endpoint"
    scrubbed = scrub_secrets(raw_error, secret_key)
    assert secret_key not in scrubbed
    assert "[REDACTED]" in scrubbed

    cfg_unconfigured = load_config(overrides={"GEMINI_API_KEY": ""})
    client_unconfigured = GeminiCloudClient(cfg_unconfigured)
    health = client_unconfigured.health_check()
    assert health["configured"] is False
    assert health["available"] is False

    with pytest.raises(AIConfigurationError):
        client_unconfigured.generate_text("system", "user")


def test_gemini_cloud_client_http_handling_and_factory() -> None:
    cfg = load_config(
        overrides={
            "AI_PROVIDER": "gemini",
            "GEMINI_API_KEY": "AIzaSyTestGeminiKey1234567890",
            "GEMINI_MODEL": "gemini-3.1-flash-lite",
            "GEMINI_MAX_RETRIES": "0",
        }
    )
    provider = create_llm_provider(cfg)
    assert isinstance(provider, GeminiCloudClient)

    mock_session = MagicMock(spec=requests.Session)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "candidates": [
            {
                "content": {
                    "parts": [{"text": "Verso de Gemini uno\nVerso de Gemini dos"}]
                }
            }
        ]
    }
    mock_session.post.return_value = mock_resp

    client = GeminiCloudClient(config=cfg, session=mock_session)
    res = client.generate_text("Sistema", "Usuario")
    assert "Verso de Gemini uno" in res.content
    assert res.model_name == "gemini-3.1-flash-lite"

    # Verificar manejo de respuesta vacía
    mock_resp.json.return_value = {"candidates": [{"content": {"parts": [{"text": "   "}]}}]}
    with pytest.raises(AIResponseError):
        client.generate_text("Sistema", "Usuario")

    # Verificar manejo de timeout
    mock_session.post.side_effect = requests.Timeout("Connection timed out")
    with pytest.raises(AIConnectionError):
        client.generate_text("Sistema", "Usuario")


@pytest.mark.skipif(
    not os.getenv("GEMINI_API_KEY")
    or os.getenv("GEMINI_API_KEY") in ("tu_gemini_api_key", "PEGAR_AQUI_TU_GEMINI_API_KEY"),
    reason="Requiere GEMINI_API_KEY real configurada en el entorno para prueba de integración externa.",
)
def test_real_gemini_cloud_integration() -> None:
    """
    Prueba de integración real contra Google Gemini API.
    """
    client = GeminiCloudClient()
    health = client.health_check()
    assert health["configured"] is True
    assert health["available"] is True

    engine = PoetryEngine(client)
    draft = engine.generate_initial_composition(seed_theme="La memoria de las estrellas")
    assert len(draft.title) >= 2
    assert len(draft.first_stanza.splitlines()) >= 2
