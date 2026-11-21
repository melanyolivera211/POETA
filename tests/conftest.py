"""
Fixtures compartidos para la suite de pruebas de POETA (tests/conftest.py).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agent.core import PoetaAgent
from api.ollama_client import AIConnectionError, LLMGenerationResult, LLMProvider
from config import AppConfig, load_config
from database.connection import DatabaseManager


class DeterministicLiteraryMockProvider(LLMProvider):
    """
    Proveedor de IA simulado exclusivamente para pruebas unitarias y de ciclo acelerado.
    Registra cada prompt recibido para auditar que la memoria persistente real fue enviada.
    """

    STANZAS = {
        1: (
            "TITULO: Liturgia del Reloj de Arena\n"
            "TEMATICA: La persistencia de la memoria frente al paso inexorable del tiempo.\n"
            "ESTROFA:\n"
            "Cae la arena fina en el cristal dormido,\n"
            "como un río secreto que numera la sombra,\n"
            "y en cada grano leve que se juzga perdido\n"
            "late un eco antiguo que en silencio nos nombra."
        ),
        2: (
            "Sobre el muro de cal se alarga la penumbra,\n"
            "hilando los minutos con agujas de plata,\n"
            "mientras una centella de recuerdos alumbra\n"
            "el nudo que la tarde lentamente desata."
        ),
        3: (
            "No es olvido la piedra que custodia el sendero,\n"
            "ni es ceniza la voz que atravesó el invierno;\n"
            "en la fragua callada del oficio sincero\n"
            "lo fugaz se detiene para volverse eterno."
        ),
        4: (
            "En la cúpide clara de la hora presente,\n"
            "convergen las mareas de lo que fue y aguarda,\n"
            "y el pulso del poema se levanta ferviente\n"
            "como lámpara viva que en la noche resguarda."
        ),
        5: (
            "Ya declina el compás hacia la orilla quieta,\n"
            "recogiendo los hilos de su trama encendida,\n"
            "cada verso trazado por la mano secreta\n"
            "ha sellado en la página su huella compartida."
        ),
        6: (
            "Se detiene el cristal al cumplirse la aurora,\n"
            "queda el canto guardado donde el tiempo no hiere,\n"
            "pues la voz que en la piedra de los siglos demora\n"
            "permanece despierta cuando el instante muere."
        ),
    }

    def __init__(self) -> None:
        self.call_count = 0
        self.recorded_prompts: list[dict[str, Any]] = []
        self.should_fail = False
        self.custom_next_response: str | None = None

    def generate_text(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.75,
    ) -> LLMGenerationResult:
        self.call_count += 1
        self.recorded_prompts.append(
            {
                "call_number": self.call_count,
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "temperature": temperature,
            }
        )

        if self.should_fail:
            raise AIConnectionError(
                "Fallo de conexión simulado con el servidor de IA.",
                duration_ms=120,
            )

        if self.custom_next_response is not None:
            content = self.custom_next_response
            self.custom_next_response = None
        else:
            intervention_idx = 1
            for i in range(2, 7):
                if f"Intervención {i} de" in user_prompt:
                    intervention_idx = i
                    break
            content = self.STANZAS[intervention_idx]

        return LLMGenerationResult(
            content=content,
            model_name="gemini-3.1-flash-lite-test",
            prompt_snapshot=f"[SYSTEM]\n{system_prompt}\n\n[USER]\n{user_prompt}",
            duration_ms=145,
            raw_metadata={"mock": True, "call": self.call_count},
        )

    def health_check(self) -> dict[str, Any]:
        return {
            "provider": "gemini",
            "configured": True,
            "available": not self.should_fail,
            "model": "gemini-3.1-flash-lite-test",
            "latency_ms": 5,
            "message": "Proveedor Gemini de pruebas listo.",
        }


@pytest.fixture()
def test_config(tmp_path: Path) -> AppConfig:
    """Configuración estándar de pruebas con reglas de 60 minutos / 10 minutos."""
    db_file = tmp_path / "poeta_test.sqlite3"
    return load_config(
        overrides={
            "APP_ENV": "testing",
            "DATABASE_URL": f"sqlite:///{db_file.as_posix()}",
            "GEMINI_API_KEY": "test-gemini-key-not-exposed",
            "GEMINI_BASE_URL": "https://generativelanguage.googleapis.com/v1beta",
            "GEMINI_MODEL": "gemini-3.1-flash-lite",
            "ADMIN_API_TOKEN": "test-admin-secret-token",
            "POETA_ACCELERATED_TEST": "false",
        }
    )


@pytest.fixture()
def accelerated_config(tmp_path: Path) -> AppConfig:
    """Configuración de modo de pruebas acelerado (ciclo de 6s, intervalo de 1s)."""
    db_file = tmp_path / "poeta_accel.sqlite3"
    return load_config(
        overrides={
            "APP_ENV": "testing",
            "DATABASE_URL": f"sqlite:///{db_file.as_posix()}",
            "GEMINI_API_KEY": "test-gemini-key-not-exposed",
            "GEMINI_BASE_URL": "https://generativelanguage.googleapis.com/v1beta",
            "GEMINI_MODEL": "gemini-3.1-flash-lite",
            "ADMIN_API_TOKEN": "",
            "POETA_ACCELERATED_TEST": "true",
            "TEST_CYCLE_DURATION_SECONDS": "6",
            "TEST_INTERVENTION_INTERVAL_SECONDS": "1",
            "WORKER_POLL_SECONDS": "1",
        }
    )


@pytest.fixture()
def db_manager(test_config: AppConfig) -> DatabaseManager:
    manager = DatabaseManager(test_config)
    manager.create_tables()
    return manager


@pytest.fixture()
def mock_llm() -> DeterministicLiteraryMockProvider:
    return DeterministicLiteraryMockProvider()


@pytest.fixture()
def poeta_agent(
    test_config: AppConfig,
    db_manager: DatabaseManager,
    mock_llm: DeterministicLiteraryMockProvider,
) -> PoetaAgent:
    return PoetaAgent(
        config=test_config,
        db_manager=db_manager,
        llm_provider=mock_llm,
    )
