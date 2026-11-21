"""
Rutas de la API REST Propia de POETA (api/routes.py).

Implementa todos los endpoints exigidos en la Sección 12:
- `POST /api/agent/start`: Inicia un ciclo de creación literaria (valida exclusividad y auth).
- `GET /api/agent/status`: Retorna el estado completo del agente, progreso y tiempos.
- `GET /api/poem/current`: Retorna la composición actual (o más reciente) y sus metadatos.
- `GET /api/poem/history`: Retorna los poemas finalizados con paginación.
- `GET /api/poem/<id>/versions`: Retorna todas las versiones de una composición.
- `GET /api/agent/logs`: Retorna el historial de actividades paginado y ordenado cronológicamente.
- `GET /api/agent/health`: Verifica el estado del backend, Supabase y Ollama Cloud.
"""

from __future__ import annotations

import hmac
import logging
import math
from typing import Any

from flask import Blueprint, Response, current_app, jsonify, request

from agent.core import PoetaAgent
from agent.memory import ActiveCycleAlreadyExistsError, PoemNotFoundError
from agent.poetry_engine import DefectivePoemResponseError
from api.ollama_client import (
    LLMProviderError,
    OllamaConfigurationError,
    OllamaConnectionError,
    OllamaResponseError,
)
from api.schemas import (
    RequestValidationError,
    parse_pagination_params,
    parse_uuid_param,
    serialize_agent_log,
    serialize_poem_detail,
    serialize_poem_version,
    validate_start_cycle_payload,
)
from config import format_bogota_display, to_bogota_iso, to_utc_iso, utc_now
from database.connection import DatabaseNotConfiguredError
from database.models import ACTIVE_POEM_STATUSES

logger = logging.getLogger("poeta.api")

api_bp = Blueprint("poeta_api", __name__, url_prefix="/api")


def _get_agent() -> PoetaAgent:
    agent: PoetaAgent = current_app.config["POETA_AGENT"]
    return agent


def _verify_admin_authorization(agent: PoetaAgent) -> tuple[bool, str]:
    """
    Protege operaciones administrativas (`POST /api/agent/start`) mediante un
    token administrativo en cabecera (`X-Admin-Token` o `Authorization: Bearer <token>`)
    comparado en tiempo constante con `hmac.compare_digest`.
    Nunca expone secretos al frontend.
    """
    if not agent.config.admin_auth_required:
        return True, ""

    expected_token = agent.config.admin_api_token
    provided_token = request.headers.get("X-Admin-Token", "").strip()
    if not provided_token:
        auth_header = request.headers.get("Authorization", "").strip()
        if auth_header.lower().startswith("bearer "):
            provided_token = auth_header[7:].strip()

    if not provided_token:
        return (
            False,
            "Operación administrativa protegida. Proporcione el token de autorización en la cabecera 'X-Admin-Token'.",
        )

    if not hmac.compare_digest(provided_token.encode("utf-8"), expected_token.encode("utf-8")):
        return False, "Credencial administrativa inválida."

    return True, ""


@api_bp.route("/agent/start", methods=["POST"])
def start_agent_cycle() -> tuple[Response, int]:
    """
    POST /api/agent/start
    Inicia un nuevo ciclo poético de 60 minutos si no existe otro activo.
    """
    agent = _get_agent()

    authorized, auth_message = _verify_admin_authorization(agent)
    if not authorized:
        return (
            jsonify(
                {
                    "status": "error",
                    "error_code": "UNAUTHORIZED",
                    "message": auth_message,
                }
            ),
            401,
        )

    try:
        payload = request.get_json(silent=True) if request.data else None
        seed_theme = validate_start_cycle_payload(payload)
    except RequestValidationError as exc:
        return (
            jsonify(
                {
                    "status": "error",
                    "error_code": "VALIDATION_ERROR",
                    "message": str(exc),
                }
            ),
            400,
        )

    try:
        poem_summary = agent.start_cycle(seed_theme=seed_theme)
        status_payload = agent.get_agent_status_payload()
        return (
            jsonify(
                {
                    "status": "success",
                    "message": "Ciclo de creación literaria POETA iniciado correctamente.",
                    "poem": poem_summary,
                    "agent": status_payload,
                }
            ),
            201,
        )
    except ActiveCycleAlreadyExistsError as exc:
        return (
            jsonify(
                {
                    "status": "error",
                    "error_code": "ACTIVE_CYCLE_EXISTS",
                    "message": str(exc),
                }
            ),
            409,
        )
    except OllamaConfigurationError as exc:
        return (
            jsonify(
                {
                    "status": "error",
                    "error_code": "AI_PROVIDER_NOT_CONFIGURED",
                    "message": str(exc),
                }
            ),
            503,
        )
    except (OllamaConnectionError, OllamaResponseError, LLMProviderError) as exc:
        return (
            jsonify(
                {
                    "status": "error",
                    "error_code": "AI_PROVIDER_ERROR",
                    "message": str(exc),
                }
            ),
            502,
        )
    except DefectivePoemResponseError as exc:
        return (
            jsonify(
                {
                    "status": "error",
                    "error_code": "DEFECTIVE_MODEL_RESPONSE",
                    "message": str(exc),
                }
            ),
            422,
        )
    except DatabaseNotConfiguredError as exc:
        return (
            jsonify(
                {
                    "status": "error",
                    "error_code": "DATABASE_NOT_CONFIGURED",
                    "message": str(exc),
                }
            ),
            503,
        )
    except Exception as exc:
        logger.exception("Error inesperado en POST /api/agent/start: %s", exc)
        return (
            jsonify(
                {
                    "status": "error",
                    "error_code": "INTERNAL_SERVER_ERROR",
                    "message": "Error interno al intentar iniciar el ciclo de creación.",
                }
            ),
            500,
        )


@api_bp.route("/agent/status", methods=["GET"])
def get_agent_status() -> tuple[Response, int]:
    """
    GET /api/agent/status
    Retorna el estado actual de la máquina de estados, el poema activo y las métricas de tiempo.
    """
    agent = _get_agent()
    try:
        payload = agent.get_agent_status_payload()
        return jsonify({"status": "success", "data": payload}), 200
    except DatabaseNotConfiguredError as exc:
        return (
            jsonify(
                {
                    "status": "error",
                    "error_code": "DATABASE_NOT_CONFIGURED",
                    "message": str(exc),
                }
            ),
            503,
        )
    except Exception as exc:
        logger.exception("Error en GET /api/agent/status: %s", exc)
        return (
            jsonify(
                {
                    "status": "error",
                    "error_code": "DATABASE_ERROR",
                    "message": f"No fue posible consultar el estado del agente: {exc}",
                }
            ),
            503,
        )


@api_bp.route("/poem/current", methods=["GET"])
def get_current_poem() -> tuple[Response, int]:
    """
    GET /api/poem/current
    Retorna la composición actual (o la última finalizada si no hay ciclo en curso)
    junto con sus metadatos y versiones.
    """
    agent = _get_agent()
    try:
        with agent.db_manager.session_scope() as session:
            poem = agent.memory_repo.get_current_or_latest_poem(session)
            if poem is None:
                return (
                    jsonify(
                        {
                            "status": "success",
                            "has_active_poem": False,
                            "poem": None,
                            "message": "Aún no se ha registrado ninguna composición poética.",
                        }
                    ),
                    200,
                )

            versions = agent.memory_repo.get_poem_versions(session, poem.id)
            is_active = poem.status in ACTIVE_POEM_STATUSES
            serialized = serialize_poem_detail(poem, versions=versions)

            return (
                jsonify(
                    {
                        "status": "success",
                        "has_active_poem": is_active,
                        "poem": serialized,
                    }
                ),
                200,
            )
    except Exception as exc:
        logger.exception("Error en GET /api/poem/current: %s", exc)
        return (
            jsonify(
                {
                    "status": "error",
                    "error_code": "CURRENT_POEM_QUERY_ERROR",
                    "message": f"No fue posible consultar el poema actual: {exc}",
                }
            ),
            503,
        )


@api_bp.route("/poem/history", methods=["GET"])
def get_poem_history() -> tuple[Response, int]:
    """
    GET /api/poem/history
    Retorna los poemas finalizados (`COMPLETED`) con soporte de paginación.
    """
    agent = _get_agent()
    try:
        page, per_page = parse_pagination_params(
            request.args.get("page"),
            request.args.get("per_page"),
            default_per_page=10,
            max_per_page=50,
        )
    except RequestValidationError as exc:
        return (
            jsonify(
                {
                    "status": "error",
                    "error_code": "VALIDATION_ERROR",
                    "message": str(exc),
                }
            ),
            400,
        )

    try:
        with agent.db_manager.session_scope() as session:
            poems, total = agent.memory_repo.get_completed_poems_paginated(
                session,
                page=page,
                per_page=per_page,
            )
            items = [serialize_poem_detail(p) for p in poems]
            total_pages = max(1, math.ceil(total / per_page)) if total > 0 else 0

            return (
                jsonify(
                    {
                        "status": "success",
                        "pagination": {
                            "page": page,
                            "per_page": per_page,
                            "total": total,
                            "total_pages": total_pages,
                        },
                        "items": items,
                    }
                ),
                200,
            )
    except Exception as exc:
        logger.exception("Error en GET /api/poem/history: %s", exc)
        return (
            jsonify(
                {
                    "status": "error",
                    "error_code": "HISTORY_QUERY_ERROR",
                    "message": f"No fue posible consultar el historial de poemas: {exc}",
                }
            ),
            503,
        )


@api_bp.route("/poem/<string:poem_id>/versions", methods=["GET"])
def get_poem_versions(poem_id: str) -> tuple[Response, int]:
    """
    GET /api/poem/{id}/versions
    Retorna todas las versiones de una composición poética específica.
    """
    agent = _get_agent()
    try:
        parsed_uuid = parse_uuid_param(poem_id, field_name="id")
    except RequestValidationError as exc:
        return (
            jsonify(
                {
                    "status": "error",
                    "error_code": "VALIDATION_ERROR",
                    "message": str(exc),
                }
            ),
            400,
        )

    try:
        with agent.db_manager.session_scope() as session:
            poem = agent.memory_repo.get_poem_by_id(session, parsed_uuid)
            versions = agent.memory_repo.get_poem_versions(session, parsed_uuid)
            return (
                jsonify(
                    {
                        "status": "success",
                        "poem": serialize_poem_detail(poem),
                        "total_versions": len(versions),
                        "versions": [serialize_poem_version(v) for v in versions],
                    }
                ),
                200,
            )
    except PoemNotFoundError as exc:
        return (
            jsonify(
                {
                    "status": "error",
                    "error_code": "POEM_NOT_FOUND",
                    "message": str(exc),
                }
            ),
            404,
        )
    except Exception as exc:
        logger.exception("Error en GET /api/poem/%s/versions: %s", poem_id, exc)
        return (
            jsonify(
                {
                    "status": "error",
                    "error_code": "VERSIONS_QUERY_ERROR",
                    "message": f"No fue posible consultar las versiones del poema: {exc}",
                }
            ),
            503,
        )


@api_bp.route("/agent/logs", methods=["GET"])
def get_agent_logs() -> tuple[Response, int]:
    """
    GET /api/agent/logs
    Retorna el historial de actividades ordenado cronológicamente con paginación.
    """
    agent = _get_agent()
    try:
        page, per_page = parse_pagination_params(
            request.args.get("page"),
            request.args.get("per_page"),
            default_per_page=25,
            max_per_page=100,
        )
        raw_poem_id = request.args.get("poem_id")
        poem_uuid = parse_uuid_param(raw_poem_id, "poem_id") if raw_poem_id else None
        event_type = request.args.get("event_type")
    except RequestValidationError as exc:
        return (
            jsonify(
                {
                    "status": "error",
                    "error_code": "VALIDATION_ERROR",
                    "message": str(exc),
                }
            ),
            400,
        )

    try:
        with agent.db_manager.session_scope() as session:
            logs, total = agent.memory_repo.get_logs_paginated(
                session,
                page=page,
                per_page=per_page,
                poem_id=poem_uuid,
                event_type=event_type,
            )
            items = [serialize_agent_log(log) for log in logs]
            total_pages = max(1, math.ceil(total / per_page)) if total > 0 else 0

            return (
                jsonify(
                    {
                        "status": "success",
                        "pagination": {
                            "page": page,
                            "per_page": per_page,
                            "total": total,
                            "total_pages": total_pages,
                        },
                        "items": items,
                    }
                ),
                200,
            )
    except Exception as exc:
        logger.exception("Error en GET /api/agent/logs: %s", exc)
        return (
            jsonify(
                {
                    "status": "error",
                    "error_code": "LOGS_QUERY_ERROR",
                    "message": f"No fue posible consultar el registro de actividades: {exc}",
                }
            ),
            503,
        )


@api_bp.route("/agent/health", methods=["GET"])
def get_agent_health() -> tuple[Response, int]:
    """
    GET /api/agent/health
    Verifica el estado real del backend, la conexión con Supabase y la disponibilidad de Ollama Cloud.
    """
    agent = _get_agent()
    now = utc_now()

    db_health = agent.db_manager.health_check()
    ai_health = agent.llm_provider.health_check()

    overall_healthy = bool(db_health.get("available")) and bool(ai_health.get("available"))
    overall_status = "healthy" if overall_healthy else "degraded"

    payload: dict[str, Any] = {
        "status": overall_status,
        "service": "poeta-api",
        "environment": agent.config.app_env,
        "display_timezone": agent.config.display_timezone,
        "timestamp_utc": to_utc_iso(now),
        "timestamp_bogota": to_bogota_iso(now),
        "timestamp_display": format_bogota_display(now),
        "components": {
            "backend": {
                "available": True,
                "message": "Servidor Flask operativo.",
            },
            "database": db_health,
            "ai_provider": ai_health,
        },
    }
    http_code = 200 if db_health.get("available") else 503
    return jsonify(payload), http_code
