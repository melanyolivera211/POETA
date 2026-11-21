"""
Módulo de integración desacoplada con Google Gemini API (api/ollama_client.py).

Implementa la interfaz `LLMProvider` y el cliente concreto `GeminiCloudClient`
para comunicarse con Google Gemini (`https://generativelanguage.googleapis.com/v1beta`).

Garantías de seguridad y resiliencia:
- Nunca expone ni imprime claves secretas en logs ni mensajes de excepción.
- Controla tiempos de espera (timeouts) y reintentos con fallback automático de modelos.
- Mide con precisión la duración de cada petición en milisegundos (`duration_ms`).
- Valida que la respuesta contenga texto poético no vacío antes de entregarla al motor literario.
"""

from __future__ import annotations

import abc
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

import requests

from config import AppConfig, load_config

logger = logging.getLogger("poeta.gemini_client")


# ==============================================================================
# EXCEPCIONES DEL PROVEEDOR DE IA
# ==============================================================================
class LLMProviderError(Exception):
    """Excepción base para errores de comunicación o respuesta del proveedor de IA."""

    def __init__(self, message: str, status_code: int | None = None, duration_ms: int = 0):
        super().__init__(message)
        self.status_code = status_code
        self.duration_ms = duration_ms


class AIConfigurationError(LLMProviderError):
    """Falta de configuración o credenciales para Google Gemini API."""


class AIConnectionError(LLMProviderError):
    """Error de red, timeout o indisponibilidad del servidor de Google Gemini."""


class AIResponseError(LLMProviderError):
    """Respuesta HTTP de error, estructura JSON inválida o contenido vacío."""


# Alias compatibles para importaciones existentes
OllamaConfigurationError = AIConfigurationError
OllamaConnectionError = AIConnectionError
OllamaResponseError = AIResponseError


# ==============================================================================
# MODELOS DE TRANSFERENCIA DEL PROVEEDOR
# ==============================================================================
@dataclass(frozen=True)
class LLMGenerationResult:
    """Resultado estandarizado de una petición de generación al modelo de lenguaje."""

    content: str
    model_name: str
    prompt_snapshot: str
    duration_ms: int
    raw_metadata: dict[str, Any]


def scrub_secrets(text: str, secret: str | None = None) -> str:
    """Elimina cualquier aparición accidental de tokens o claves de API en logs."""
    if not text:
        return ""
    cleaned = re.sub(r"Bearer\s+[A-Za-z0-9_\-\.]+", "Bearer [REDACTED]", text)
    cleaned = re.sub(r"AIza[A-Za-z0-9_\-]{20,}", "AIza[REDACTED]", cleaned)
    cleaned = re.sub(r"AQ\.[A-Za-z0-9_\-\.]{20,}", "AQ.[REDACTED]", cleaned)
    cleaned = re.sub(r"key=[A-Za-z0-9_\-\.]+", "key=[REDACTED]", cleaned)
    if secret and len(secret) >= 4:
        cleaned = cleaned.replace(secret, "[REDACTED]")
    return cleaned


# ==============================================================================
# INTERFAZ ABSTRACTA DEL PROVEEDOR DE IA (DESACOPLAMIENTO)
# ==============================================================================
class LLMProvider(abc.ABC):
    """
    Interfaz de proveedor de modelos de lenguaje desacoplada del motor del agente.
    """

    @abc.abstractmethod
    def generate_text(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.75,
    ) -> LLMGenerationResult:
        """Envía el contexto al modelo y retorna la generación sincrónica."""

    @abc.abstractmethod
    def health_check(self) -> dict[str, Any]:
        """Verifica la configuración y disponibilidad real del proveedor de IA."""


# ==============================================================================
# CLIENTE OFICIAL PARA GOOGLE GEMINI API
# ==============================================================================
class GeminiCloudClient(LLMProvider):
    """
    Cliente HTTP REST para Google Gemini API (`https://generativelanguage.googleapis.com/v1beta`).
    """

    FALLBACK_MODELS: tuple[str, ...] = (
        "gemini-3.1-flash-lite",
        "gemini-3.5-flash-lite",
        "gemma-4-26b-a4b-it",
        "gemini-3.8-flash",
    )

    def __init__(
        self,
        config: AppConfig | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self.config = config or load_config()
        self.base_url = self.config.gemini_base_url.rstrip("/")
        self.api_key = self.config.gemini_api_key
        self.model = self._normalize_model_name(self.config.gemini_model)
        self.timeout_seconds = max(5, int(self.config.ai_timeout_seconds))
        self.max_retries = max(0, int(self.config.ai_max_retries))
        self.session = session or requests.Session()

    @staticmethod
    def _normalize_model_name(raw_model: str) -> str:
        model = (raw_model or "gemini-3.1-flash-lite").strip()
        if model.startswith("models/"):
            model = model[len("models/") :]
        return model

    def _build_headers(self) -> dict[str, str]:
        if not self.config.has_gemini_configured:
            raise AIConfigurationError(
                "GEMINI_API_KEY no está configurada. Configure una clave válida de Google Gemini "
                "en el archivo .env."
            )
        return {
            "x-goog-api-key": self.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "POETA-Autonomous-Literary-Agent/1.0",
        }

    def _candidate_models(self) -> list[str]:
        candidates = [self.model]
        for fb in self.FALLBACK_MODELS:
            if fb not in candidates:
                candidates.append(fb)
        return candidates

    def generate_text(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.75,
    ) -> LLMGenerationResult:
        """
        Ejecuta una petición sincrónica a Google Gemini API (`:generateContent`).
        """
        headers = self._build_headers()

        payload: dict[str, Any] = {
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": user_prompt.strip()}],
                }
            ],
            "generationConfig": {
                "temperature": temperature,
            },
        }
        if system_prompt and system_prompt.strip():
            payload["systemInstruction"] = {
                "parts": [{"text": system_prompt.strip()}],
            }

        prompt_snapshot = f"[SYSTEM]\n{system_prompt.strip()}\n\n[USER]\n{user_prompt.strip()}"
        last_error: LLMProviderError | None = None
        overall_start = time.perf_counter()

        models_to_try = self._candidate_models()

        for model_name in models_to_try:
            endpoint = f"{self.base_url}/models/{model_name}:generateContent"
            for attempt in range(self.max_retries + 1):
                attempt_start = time.perf_counter()
                try:
                    response = self.session.post(
                        endpoint,
                        headers=headers,
                        json=payload,
                        timeout=self.timeout_seconds,
                    )
                    duration_ms = int((time.perf_counter() - attempt_start) * 1000)

                    if response.status_code in (404, 429, 503) and model_name != models_to_try[-1]:
                        logger.warning(
                            "Modelo Gemini '%s' devolvió HTTP %d; probando modelo alternativo...",
                            model_name,
                            response.status_code,
                        )
                        err_detail = scrub_secrets(response.text[:300], self.api_key)
                        last_error = AIResponseError(
                            f"Gemini API (HTTP {response.status_code}) en {model_name}: {err_detail}",
                            status_code=response.status_code,
                            duration_ms=duration_ms,
                        )
                        break

                    if response.status_code >= 500:
                        err_msg = scrub_secrets(
                            f"Error del servidor Google Gemini API (HTTP {response.status_code}).",
                            self.api_key,
                        )
                        last_error = AIConnectionError(
                            err_msg,
                            status_code=response.status_code,
                            duration_ms=duration_ms,
                        )
                        if attempt < self.max_retries:
                            time.sleep(min(2**attempt, 4))
                            continue
                        break

                    if response.status_code >= 400:
                        err_detail = scrub_secrets(response.text[:300], self.api_key)
                        raise AIResponseError(
                            f"Google Gemini API rechazó la petición (HTTP {response.status_code}): {err_detail}",
                            status_code=response.status_code,
                            duration_ms=duration_ms,
                        )

                    try:
                        data = response.json()
                    except ValueError as exc:
                        raise AIResponseError(
                            "Google Gemini API devolvió una respuesta que no es JSON válido.",
                            status_code=response.status_code,
                            duration_ms=duration_ms,
                        ) from exc

                    extracted_text = self._extract_gemini_text(data)
                    if not extracted_text or not extracted_text.strip():
                        raise AIResponseError(
                            "Google Gemini API devolvió una respuesta vacía.",
                            status_code=response.status_code,
                            duration_ms=duration_ms,
                        )

                    return LLMGenerationResult(
                        content=extracted_text.strip(),
                        model_name=model_name,
                        prompt_snapshot=prompt_snapshot,
                        duration_ms=duration_ms,
                        raw_metadata={
                            "provider": "gemini",
                            "endpoint": endpoint,
                            "status_code": response.status_code,
                            "attempt": attempt + 1,
                        },
                    )

                except (requests.Timeout, requests.ConnectionError) as exc:
                    duration_ms = int((time.perf_counter() - attempt_start) * 1000)
                    safe_msg = scrub_secrets(str(exc), self.api_key)
                    last_error = AIConnectionError(
                        f"No fue posible conectar con Google Gemini API tras {duration_ms} ms: {safe_msg}",
                        duration_ms=duration_ms,
                    )
                    if attempt < self.max_retries:
                        time.sleep(min(2**attempt, 4))
                        continue
                    raise last_error from exc
                except requests.RequestException as exc:
                    duration_ms = int((time.perf_counter() - attempt_start) * 1000)
                    safe_msg = scrub_secrets(str(exc), self.api_key)
                    raise AIConnectionError(
                        f"Error HTTP hacia Google Gemini API: {safe_msg}",
                        duration_ms=duration_ms,
                    ) from exc

        total_ms = int((time.perf_counter() - overall_start) * 1000)
        raise last_error or AIConnectionError(
            "Agotados los reintentos de comunicación con Google Gemini API.",
            duration_ms=total_ms,
        )

    @staticmethod
    def _extract_gemini_text(data: dict[str, Any]) -> str:
        if not isinstance(data, dict):
            return ""
        candidates = data.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            return ""
        first = candidates[0]
        if not isinstance(first, dict):
            return ""
        content = first.get("content")
        if not isinstance(content, dict):
            return ""
        parts = content.get("parts")
        if not isinstance(parts, list):
            return ""
        texts = [
            str(p.get("text"))
            for p in parts
            if isinstance(p, dict)
            and isinstance(p.get("text"), str)
            and not p.get("thought", False)
        ]
        return "\n".join(texts).strip()

    def health_check(self) -> dict[str, Any]:
        """Verifica la configuración y conexión real con Google Gemini API."""
        if not self.config.has_gemini_configured:
            return {
                "provider": "gemini",
                "configured": False,
                "available": False,
                "base_url": self.base_url,
                "model": self.model,
                "message": "GEMINI_API_KEY no configurada en el archivo .env.",
            }

        start = time.perf_counter()
        try:
            headers = self._build_headers()
            response = self.session.get(
                f"{self.base_url}/models?pageSize=5",
                headers=headers,
                timeout=min(10, self.timeout_seconds),
            )
            latency_ms = int((time.perf_counter() - start) * 1000)
            if response.status_code == 200:
                return {
                    "provider": "gemini",
                    "configured": True,
                    "available": True,
                    "base_url": self.base_url,
                    "model": self.model,
                    "latency_ms": latency_ms,
                    "message": f"Conexión verificada con Google Gemini ({self.model}).",
                }
            return {
                "provider": "gemini",
                "configured": True,
                "available": False,
                "base_url": self.base_url,
                "model": self.model,
                "status_code": response.status_code,
                "latency_ms": latency_ms,
                "message": f"Google Gemini API respondió con HTTP {response.status_code}.",
            }
        except Exception as exc:
            latency_ms = int((time.perf_counter() - start) * 1000)
            return {
                "provider": "gemini",
                "configured": True,
                "available": False,
                "base_url": self.base_url,
                "model": self.model,
                "latency_ms": latency_ms,
                "message": scrub_secrets(str(exc), self.api_key),
            }


def create_llm_provider(config: AppConfig | None = None) -> LLMProvider:
    """Instancia el cliente de IA Google Gemini configurado."""
    cfg = config or load_config()
    return GeminiCloudClient(cfg)
