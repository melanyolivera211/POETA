"""
Pruebas de los Endpoints de la API REST Flask y Seguridad (tests/test_api.py).

Verifica todos los endpoints de la Sección 12:
- `POST /api/agent/start` (incluyendo autenticación con `X-Admin-Token` y bloqueo 409 Conflict).
- `GET /api/agent/status`
- `GET /api/poem/current`
- `GET /api/poem/history` (con paginación)
- `GET /api/poem/{id}/versions`
- `GET /api/agent/logs` (con paginación y trazabilidad de fechas en America/Bogota)
- `GET /api/agent/health`
- `GET /` (entrega del Dashboard HTML editorial)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agent.core import PoetaAgent
from app import create_app
from config import AppConfig
from database.connection import DatabaseManager
from tests.conftest import DeterministicLiteraryMockProvider


@pytest.fixture()
def client(
    test_config: AppConfig,
    db_manager: DatabaseManager,
    mock_llm: DeterministicLiteraryMockProvider,
    poeta_agent: PoetaAgent,
):
    flask_app = create_app(
        config=test_config,
        db_manager=db_manager,
        llm_provider=mock_llm,
        agent=poeta_agent,
    )
    flask_app.config["TESTING"] = True
    with flask_app.test_client() as test_client:
        yield test_client


def test_dashboard_html_and_health_endpoint(client) -> None:
    res_html = client.get("/")
    assert res_html.status_code == 200
    assert b"POETA" in res_html.data

    res_health = client.get("/api/agent/health")
    assert res_health.status_code == 200
    data = res_health.get_json()
    assert data["status"] == "healthy"
    assert data["components"]["backend"]["available"] is True
    assert data["components"]["database"]["available"] is True
    assert data["components"]["ai_provider"]["available"] is True


def test_admin_auth_protection_and_start_cycle_exclusivity(client) -> None:
    # 1. Sin X-Admin-Token debe rechazar con HTTP 401
    res_unauth = client.post("/api/agent/start", json={"seed_theme": "El mar"})
    assert res_unauth.status_code == 401
    assert res_unauth.get_json()["error_code"] == "UNAUTHORIZED"

    # 2. Con X-Admin-Token válido inicia el ciclo con HTTP 201
    headers = {"X-Admin-Token": "test-admin-secret-token"}
    res_start = client.post(
        "/api/agent/start",
        headers=headers,
        json={"seed_theme": "La biblioteca infinita"},
    )
    assert res_start.status_code == 201
    start_data = res_start.get_json()
    assert start_data["status"] == "success"
    poem_id = start_data["poem"]["id"]
    assert start_data["poem"]["completed_interventions"] == 1

    # 3. Intentar iniciar otro ciclo mientras hay uno activo debe devolver HTTP 409 Conflict
    res_conflict = client.post(
        "/api/agent/start",
        headers=headers,
        json={"seed_theme": "Segundo poema simultáneo"},
    )
    assert res_conflict.status_code == 409
    assert res_conflict.get_json()["error_code"] == "ACTIVE_CYCLE_EXISTS"

    # 4. Consultar GET /api/agent/status
    res_status = client.get("/api/agent/status")
    assert res_status.status_code == 200
    status_payload = res_status.get_json()["data"]
    assert status_payload["is_cycle_active"] is True
    assert status_payload["active_poem"]["id"] == poem_id
    assert status_payload["intervention_number"] == 1
    assert status_payload["cycle_duration_minutes"] == 60

    # 5. Consultar GET /api/poem/current
    res_current = client.get("/api/poem/current")
    assert res_current.status_code == 200
    curr_data = res_current.get_json()
    assert curr_data["has_active_poem"] is True
    assert curr_data["poem"]["id"] == poem_id
    assert len(curr_data["poem"]["versions"]) == 1

    # 6. Consultar GET /api/poem/{id}/versions
    res_versions = client.get(f"/api/poem/{poem_id}/versions")
    assert res_versions.status_code == 200
    ver_data = res_versions.get_json()
    assert ver_data["total_versions"] == 1
    assert ver_data["versions"][0]["intervention_number"] == 1
    assert ver_data["versions"][0]["generated_at_bogota"] is not None

    # 7. Consultar GET /api/agent/logs con paginación
    res_logs = client.get("/api/agent/logs?page=1&per_page=10")
    assert res_logs.status_code == 200
    logs_data = res_logs.get_json()
    assert logs_data["pagination"]["total"] >= 4
    first_log = logs_data["items"][0]
    assert first_log["created_at_bogota"] is not None
    assert "scheduled_at_bogota" in first_log["details"]


def test_history_endpoint_returns_completed_poems(
    client,
    poeta_agent: PoetaAgent,
) -> None:
    t0 = datetime(2026, 11, 21, 9, 0, 0, tzinfo=timezone.utc)
    poem = poeta_agent.start_cycle(current_time=t0)
    for minute in [10, 20, 30, 40, 50, 60]:
        poeta_agent.process_scheduled_tick(current_time=t0 + timedelta(minutes=minute))

    res_history = client.get("/api/poem/history?page=1&per_page=10")
    assert res_history.status_code == 200
    hist_data = res_history.get_json()
    assert hist_data["pagination"]["total"] == 1
    assert hist_data["items"][0]["id"] == poem["id"]
    assert hist_data["items"][0]["status"] == "COMPLETED"
    assert hist_data["items"][0]["completed_interventions"] == 6
