"""
Módulo de integración desacoplada con Ollama Cloud (api/ollama_client.py).

Implementa la interfaz `LLMProvider` y el cliente concreto `OllamaCloudClient`
para comunicarse con el servicio oficial de Ollama Cloud (`https://ollama.com/api`).

Especificaciones técnicas de Ollama Cloud verificadas:
- Base URL nativa: `https://ollama.com/api` (o endpoint compatible `/v1/chat/completions`)
- Endpoint de generación: `POST https://ollama.com/api/chat` con `"stream": false`
- Endpoint de verificación/modelos: `GET https://ollama.com/api/tags`
- Autenticación: Cabecera HTTP `Authorization: Bearer <OLLAMA_API_KEY>`

Garantías de seguridad y resiliencia:
- Nunca expone ni imprime tokens secretos en logs ni mensajes de excepción.
- Controla tiempos de espera (timeouts) y reintentos con backoff ante errores transitorios.
- Mide con precisión la duración de cada petición en milisegundos (`duration_ms`).
- Valida que la respuesta contenga texto no vacío antes de entregarla al motor literario.
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

logger = logging.getLogger("poeta.ollama_client")


# ==============================================================================
# EXCEPCIONES DEL PROVEEDOR DE IA
# ==============================================================================
class LLMProviderError(Exception):
    """Excepción base para errores de comunicación o respuesta del proveedor de IA."""

    def __init__(self, message: str, status_code: int | None = None, duration_ms: int = 0):
        super().__init__(message)
        self.status_code = status_code
        self.duration_ms = duration_ms


class OllamaConfigurationError(LLMProviderError):
    """Falta de configuración o credenciales para Ollama Cloud."""


class OllamaConnectionError(LLMProviderError):
    """Error de red, timeout o indisponibilidad del servidor de Ollama Cloud."""


class OllamaResponseError(LLMProviderError):
    """Respuesta HTTP de error, estructura JSON inválida o contenido vacío."""


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
    """Elimina cualquier aparición accidental de tokens o claves Bearer en textos de log."""
    if not text:
        return ""
    cleaned = re.sub(r"Bearer\s+[A-Za-z0-9_\-\.]+", "Bearer [REDACTED]", text)
    if secret and len(secret) >= 4:
        cleaned = cleaned.replace(secret, "[REDACTED]")
    return cleaned


# ==============================================================================
# INTERFAZ ABSTRACTA DEL PROVEEDOR DE IA (DESACOPLAMIENTO)
# ==============================================================================
class LLMProvider(abc.ABC):
    """
    Interfaz de proveedor de modelos de lenguaje desacoplada del motor del agente.
    Permite cambiar entre Ollama Cloud y otro proveedor compatible sin reescribir
    la memoria persistente ni el planificador.
    """

    @abc.abstractmethod
    def generate_text(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.75,
    ) -> LLMGenerationResult:
        """Envía el contexto al modelo y retorna la generación no transmitida en streaming."""

    @abc.abstractmethod
    def health_check(self) -> dict[str, Any]:
        """Verifica la configuración y disponibilidad real del proveedor de IA."""


# ==============================================================================
# CLIENTE OFICIAL PARA OLLAMA CLOUD
# ==============================================================================
class OllamaCloudClient(LLMProvider):
    """
    Cliente HTTP para Ollama Cloud (`https://ollama.com/api`).

    No asume la existencia de un servidor Ollama local (`localhost:11434`).
    Requiere autenticación mediante `OLLAMA_API_KEY` con esquema Bearer Token.
    """

    def __init__(
        self,
        config: AppConfig | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self.config = config or load_config()
        self.base_url = self._normalize_base_url(self.config.ollama_base_url)
        self.api_key = self.config.ollama_api_key
        self.model = self.config.ollama_model
        self.timeout_seconds = max(5, int(self.config.ollama_timeout_seconds))
        self.max_retries = max(0, int(self.config.ollama_max_retries))
        self.session = session or requests.Session()

    @staticmethod
    def _normalize_base_url(raw_url: str) -> str:
        """
        Normaliza la URL base de Ollama Cloud:
        - Si el usuario configura `https://ollama.com`, se ajusta a `https://ollama.com/api`.
        - Si ya termina en `/api` o `/v1`, se conserva.
        """
        url = (raw_url or "https://ollama.com/api").strip().rstrip("/")
        if url == "https://ollama.com":
            return "https://ollama.com/api"
        return url

    @property
    def is_openai_compatible_endpoint(self) -> bool:
        """Detecta si la URL base apunta al endpoint compatible con OpenAI (`/v1`)."""
        return self.base_url.endswith("/v1")

    @property
    def chat_endpoint(self) -> str:
        if self.is_openai_compatible_endpoint:
            return f"{self.base_url}/chat/completions"
        if self.base_url.endswith("/api"):
            return f"{self.base_url}/chat"
        return f"{self.base_url}/api/chat"

    @property
    def tags_endpoint(self) -> str:
        if self.is_openai_compatible_endpoint:
            return f"{self.base_url}/models"
        if self.base_url.endswith("/api"):
            return f"{self.base_url}/tags"
        return f"{self.base_url}/api/tags"

    def _build_headers(self) -> dict[str, str]:
        """Construye las cabeceras HTTP autenticadas sin exponer la clave en logs."""
        if not self.config.has_ollama_configured:
            raise OllamaConfigurationError(
                "OLLAMA_API_KEY no está configurada. Configure una clave válida de Ollama Cloud "
                "(https://ollama.com/settings/keys) en las variables de entorno."
            )
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "POETA-Autonomous-Literary-Agent/1.0",
        }

    def generate_text(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.75,
    ) -> LLMGenerationResult:
        """
        Ejecuta una petición sincrónica (`stream: false`) a Ollama Cloud.
        Controla tiempos de espera, errores HTTP, reintentos y validación de texto.
        """
        headers = self._build_headers()

        messages = []
        if system_prompt and system_prompt.strip():
            messages.append({"role": "system", "content": system_prompt.strip()})
        messages.append({"role": "user", "content": user_prompt.strip()})

        if self.is_openai_compatible_endpoint:
            payload: dict[str, Any] = {
                "model": self.model,
                "messages": messages,
                "stream": False,
                "temperature": temperature,
            }
        else:
            payload = {
                "model": self.model,
                "messages": messages,
                "stream": False,
                "options": {
                    "temperature": temperature,
                },
            }

        prompt_snapshot = f"[SYSTEM]\n{system_prompt.strip()}\n\n[USER]\n{user_prompt.strip()}"
        last_error: LLMProviderError | None = None
        overall_start = time.perf_counter()

        for attempt in range(self.max_retries + 1):
            attempt_start = time.perf_counter()
            try:
                response = self.session.post(
                    self.chat_endpoint,
                    headers=headers,
                    json=payload,
                    timeout=self.timeout_seconds,
                )
                duration_ms = int((time.perf_counter() - attempt_start) * 1000)

                if response.status_code >= 500:
                    err_msg = scrub_secrets(
                        f"Error del servidor Ollama Cloud (HTTP {response.status_code}).",
                        self.api_key,
                    )
                    last_error = OllamaConnectionError(
                        err_msg,
                        status_code=response.status_code,
                        duration_ms=duration_ms,
                    )
                    if attempt < self.max_retries:
                        time.sleep(min(2**attempt, 4))
                        continue
                    raise last_error

                if response.status_code >= 400:
                    err_detail = scrub_secrets(response.text[:300], self.api_key)
                    raise OllamaResponseError(
                        f"Ollama Cloud rechazó la petición (HTTP {response.status_code}): {err_detail}",
                        status_code=response.status_code,
                        duration_ms=duration_ms,
                    )

                try:
                    data = response.json()
                except ValueError as exc:
                    raise OllamaResponseError(
                        "Ollama Cloud devolvió una respuesta que no es JSON válido.",
                        status_code=response.status_code,
                        duration_ms=duration_ms,
                    ) from exc

                extracted_text = self._extract_text(data)
                if not extracted_text or not extracted_text.strip():
                    raise OllamaResponseError(
                        "Ollama Cloud devolvió una respuesta vacía o sin contenido de texto.",
                        status_code=response.status_code,
                        duration_ms=duration_ms,
                    )

                returned_model = str(data.get("model") or self.model)
                return LLMGenerationResult(
                    content=extracted_text.strip(),
                    model_name=returned_model,
                    prompt_snapshot=prompt_snapshot,
                    duration_ms=duration_ms,
                    raw_metadata={
                        "endpoint": self.chat_endpoint,
                        "status_code": response.status_code,
                        "attempt": attempt + 1,
                        "done": data.get("done", True),
                    },
                )

            except (requests.Timeout, requests.ConnectionError) as exc:
                duration_ms = int((time.perf_counter() - attempt_start) * 1000)
                safe_msg = scrub_secrets(str(exc), self.api_key)
                last_error = OllamaConnectionError(
                    f"No fue posible conectar con Ollama Cloud tras {duration_ms} ms: {safe_msg}",
                    duration_ms=duration_ms,
                )
                logger.warning(
                    "Fallo de conexión con Ollama Cloud (intento %d/%d): %s",
                    attempt + 1,
                    self.max_retries + 1,
                    safe_msg,
                )
                if attempt < self.max_retries:
                    time.sleep(min(2**attempt, 4))
                    continue
                raise last_error from exc
            except requests.RequestException as exc:
                duration_ms = int((time.perf_counter() - attempt_start) * 1000)
                safe_msg = scrub_secrets(str(exc), self.api_key)
                raise OllamaConnectionError(
                    f"Error en cliente HTTP hacia Ollama Cloud: {safe_msg}",
                    duration_ms=duration_ms,
                ) from exc

        total_ms = int((time.perf_counter() - overall_start) * 1000)
        raise last_error or OllamaConnectionError(
            "Agotados los reintentos de comunicación con Ollama Cloud.",
            duration_ms=total_ms,
        )

    @staticmethod
    def _extract_text(data: dict[str, Any]) -> str:
        """
        Extrae el texto generado tanto del formato nativo de Ollama (`/api/chat` o `/api/generate`)
        como del formato compatible con OpenAI (`/v1/chat/completions`).
        """
        if not isinstance(data, dict):
            return ""

        # 1. Formato nativo Ollama /api/chat: {"message": {"role": "assistant", "content": "..."}}
        message = data.get("message")
        if isinstance(message, dict) and isinstance(message.get("content"), str):
            return message["content"]

        # 2. Formato nativo Ollama /api/generate: {"response": "..."}
        if isinstance(data.get("response"), str):
            return data["response"]

        # 3. Formato compatible OpenAI /v1/chat/completions: {"choices": [{"message": {"content": "..."}}]}
        choices = data.get("choices")
        if isinstance(choices, list) and len(choices) > 0:
            first_choice = choices[0]
            if isinstance(first_choice, dict):
                choice_msg = first_choice.get("message")
                if isinstance(choice_msg, dict) and isinstance(choice_msg.get("content"), str):
                    return choice_msg["content"]
                if isinstance(first_choice.get("text"), str):
                    return first_choice["text"]

        return ""

    def health_check(self) -> dict[str, Any]:
        """
        Verifica el estado de la integración con Ollama Cloud sin simular éxito.
        Si falta `OLLAMA_API_KEY`, informa explícitamente `configured: False`.
        """
        if not self.config.has_ollama_configured:
            return {
                "provider": "ollama_cloud",
                "configured": False,
                "available": False,
                "base_url": self.base_url,
                "model": self.model,
                "message": "OLLAMA_API_KEY no configurada en el entorno.",
            }

        start = time.perf_counter()
        try:
            headers = self._build_headers()
            response = self.session.get(
                self.tags_endpoint,
                headers=headers,
                timeout=min(10, self.timeout_seconds),
            )
            latency_ms = int((time.perf_counter() - start) * 1000)

            if response.status_code == 200:
                return {
                    "provider": "ollama_cloud",
                    "configured": True,
                    "available": True,
                    "base_url": self.base_url,
                    "model": self.model,
                    "latency_ms": latency_ms,
                    "message": "Conexión verificada con Ollama Cloud.",
                }

            return {
                "provider": "ollama_cloud",
                "configured": True,
                "available": False,
                "base_url": self.base_url,
                "model": self.model,
                "status_code": response.status_code,
                "latency_ms": latency_ms,
                "message": f"Ollama Cloud respondió con HTTP {response.status_code}.",
            }
        except Exception as exc:
            latency_ms = int((time.perf_counter() - start) * 1000)
            return {
                "provider": "ollama_cloud",
                "configured": True,
                "available": False,
                "base_url": self.base_url,
                "model": self.model,
                "latency_ms": latency_ms,
                "message": scrub_secrets(str(exc), self.api_key),
            }
