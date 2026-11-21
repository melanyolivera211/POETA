"""
Esquemas de serialización y validación de entradas para la API REST de POETA (api/schemas.py).

Garantiza respuestas JSON consistentes, validación segura de parámetros de entrada,
paginación estandarizada y conversión de marcas temporales a America/Bogota.
"""

from __future__ import annotations

import re
from typing import Any
import uuid

from config import format_bogota_display, to_bogota_iso, to_utc_iso
from database.models import AgentLog, Poem, PoemVersion


class RequestValidationError(ValueError):
    """Excepción lanzada cuando los parámetros de entrada de una petición son inválidos."""


def validate_start_cycle_payload(payload: dict[str, Any] | None) -> str | None:
    """
    Valida y sanea el payload opcional de `POST /api/agent/start`.
    Retorna `seed_theme` saneado o `None`.
    """
    if not payload:
        return None
    if not isinstance(payload, dict):
        raise RequestValidationError("El cuerpo de la petición debe ser un objeto JSON válido.")

    raw_theme = payload.get("seed_theme") or payload.get("theme")
    if raw_theme is None:
        return None
    if not isinstance(raw_theme, str):
        raise RequestValidationError("El campo 'seed_theme' debe ser una cadena de texto.")

    cleaned = re.sub(r"[\x00-\x1f\x7f]+", " ", raw_theme).strip()
    if len(cleaned) > 300:
        raise RequestValidationError(
            "La temática o inspiración inicial no puede exceder los 300 caracteres."
        )
    return cleaned or None


def parse_uuid_param(raw_id: str, field_name: str = "id") -> uuid.UUID:
    """Valida que un identificador de ruta o query string sea un UUID válido."""
    try:
        return uuid.UUID(str(raw_id).strip())
    except (ValueError, TypeError) as exc:
        raise RequestValidationError(
            f"El parámetro '{field_name}' debe ser un UUID válido."
        ) from exc


def parse_pagination_params(
    raw_page: Any,
    raw_per_page: Any,
    *,
    default_per_page: int = 20,
    max_per_page: int = 100,
) -> tuple[int, int]:
    """Valida y acota los parámetros de paginación `page` y `per_page`."""
    try:
        page = int(raw_page) if raw_page is not None else 1
    except (ValueError, TypeError) as exc:
        raise RequestValidationError("El parámetro 'page' debe ser un número entero.") from exc

    try:
        per_page = int(raw_per_page) if raw_per_page is not None else default_per_page
    except (ValueError, TypeError) as exc:
        raise RequestValidationError("El parámetro 'per_page' debe ser un número entero.") from exc

    if page < 1:
        raise RequestValidationError("El parámetro 'page' debe ser mayor o igual a 1.")
    if per_page < 1 or per_page > max_per_page:
        raise RequestValidationError(
            f"El parámetro 'per_page' debe estar entre 1 y {max_per_page}."
        )
    return page, per_page


def serialize_poem_version(version: PoemVersion) -> dict[str, Any]:
    """Serializa una fila de `poem_versions` con marcas temporales en UTC y America/Bogota."""
    stanzas = [s.strip() for s in (version.full_text or "").split("\n\n") if s.strip()]
    return {
        "id": str(version.id),
        "poem_id": str(version.poem_id),
        "intervention_number": version.intervention_number,
        "added_stanza": version.added_stanza,
        "full_text": version.full_text,
        "stanzas_count": len(stanzas),
        "model_name": version.model_name,
        "prompt_snapshot": version.prompt_snapshot,
        "generated_at_utc": to_utc_iso(version.generated_at),
        "generated_at_bogota": to_bogota_iso(version.generated_at),
        "generated_at_display": format_bogota_display(version.generated_at),
        "generation_duration_ms": version.generation_duration_ms,
        "generation_status": version.generation_status,
    }


def serialize_poem_detail(
    poem: Poem,
    versions: list[PoemVersion] | None = None,
) -> dict[str, Any]:
    """Serializa un objeto `Poem` junto con sus estrofas desglosadas y versiones opcionales."""
    stanzas = [s.strip() for s in (poem.full_text or "").split("\n\n") if s.strip()]
    data: dict[str, Any] = {
        "id": str(poem.id),
        "title": poem.title,
        "theme": poem.theme,
        "initial_prompt": poem.initial_prompt,
        "status": poem.status,
        "full_text": poem.full_text,
        "stanzas": stanzas,
        "total_interventions": poem.total_interventions,
        "completed_interventions": poem.completed_interventions,
        "started_at_utc": to_utc_iso(poem.started_at),
        "started_at_bogota": to_bogota_iso(poem.started_at),
        "started_at_display": format_bogota_display(poem.started_at),
        "scheduled_end_at_utc": to_utc_iso(poem.scheduled_end_at),
        "scheduled_end_at_bogota": to_bogota_iso(poem.scheduled_end_at),
        "scheduled_end_at_display": format_bogota_display(poem.scheduled_end_at),
        "next_execution_at_utc": to_utc_iso(poem.next_execution_at),
        "next_execution_at_bogota": to_bogota_iso(poem.next_execution_at),
        "next_execution_at_display": format_bogota_display(poem.next_execution_at),
        "completed_at_utc": to_utc_iso(poem.completed_at),
        "completed_at_bogota": to_bogota_iso(poem.completed_at),
        "completed_at_display": format_bogota_display(poem.completed_at),
        "created_at_utc": to_utc_iso(poem.created_at),
        "created_at_bogota": to_bogota_iso(poem.created_at),
        "created_at_display": format_bogota_display(poem.created_at),
        "updated_at_utc": to_utc_iso(poem.updated_at),
        "updated_at_bogota": to_bogota_iso(poem.updated_at),
        "updated_at_display": format_bogota_display(poem.updated_at),
    }
    if versions is not None:
        data["versions"] = [serialize_poem_version(v) for v in versions]
    return data


def serialize_agent_log(log: AgentLog) -> dict[str, Any]:
    """Serializa una entrada de `agent_logs` incluyendo trazabilidad temporal detallada."""
    details = log.details or {}
    return {
        "id": str(log.id),
        "poem_id": str(log.poem_id) if log.poem_id else None,
        "event_type": log.event_type,
        "description": log.description,
        "status": log.status,
        "details": details,
        "created_at_utc": to_utc_iso(log.created_at),
        "created_at_bogota": to_bogota_iso(log.created_at),
        "created_at_display": format_bogota_display(log.created_at),
        "scheduled_at_bogota": details.get("scheduled_at_bogota"),
        "actual_execution_at_bogota": details.get("actual_execution_at_bogota"),
        "finished_at_bogota": details.get("finished_at_bogota"),
    }
