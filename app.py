"""
Servidor Web Flask y Punto de Entrada de `poeta-api` (app.py).

Responsabilidades (Sección 6, Sección 12 y Sección 17):
- Exponer la API REST propia (`/api/...`).
- Servir el Dashboard HTML5/CSS3/JS editorial (`frontend/`).
- Configurar CORS para los orígenes autorizados.
- Mantener el servidor HTTP completamente separado del proceso Background Worker (`worker.py`),
  evitando iniciar APScheduler dentro de múltiples workers de Gunicorn.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, send_from_directory
from flask_cors import CORS

from agent.core import PoetaAgent
from api.ollama_client import LLMProvider, OllamaCloudClient
from api.routes import api_bp
from config import AppConfig, load_config
from database.connection import DatabaseManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("poeta.app")

FRONTEND_DIR = Path(__file__).resolve().parent / "frontend"


def create_app(
    config: AppConfig | None = None,
    db_manager: DatabaseManager | None = None,
    llm_provider: LLMProvider | None = None,
    agent: PoetaAgent | None = None,
) -> Flask:
    """Factory function para crear y configurar la aplicación Flask de POETA."""
    app_config = config or load_config()

    app = Flask(
        __name__,
        static_folder=str(FRONTEND_DIR),
        static_url_path="",
    )
    app.config["SECRET_KEY"] = app_config.secret_key
    app.config["JSON_SORT_KEYS"] = False

    # Configuración de CORS
    origins_raw = app_config.cors_allowed_origins
    origins: str | list[str] = (
        "*"
        if origins_raw == "*"
        else [o.strip() for o in origins_raw.split(",") if o.strip()]
    )
    CORS(app, resources={r"/api/*": {"origins": origins}})

    # Inicializar dependencias desacopladas
    resolved_db = db_manager or DatabaseManager(app_config)
    if app_config.has_database_configured or app_config.is_testing:
        try:
            resolved_db.create_tables()
        except Exception as exc:
            logger.warning("No se pudieron inicializar las tablas automáticamente: %s", exc)

    resolved_llm = llm_provider or OllamaCloudClient(app_config)
    resolved_agent = agent or PoetaAgent(
        config=app_config,
        db_manager=resolved_db,
        llm_provider=resolved_llm,
    )

    app.config["POETA_CONFIG"] = app_config
    app.config["POETA_DB"] = resolved_db
    app.config["POETA_LLM"] = resolved_llm
    app.config["POETA_AGENT"] = resolved_agent

    # Registrar Blueprint de la API REST
    app.register_blueprint(api_bp)

    # Rutas del Dashboard HTML
    @app.route("/", methods=["GET"])
    def serve_dashboard() -> Response:
        return send_from_directory(str(FRONTEND_DIR), "index.html")

    @app.route("/css/<path:filename>", methods=["GET"])
    def serve_css(filename: str) -> Response:
        return send_from_directory(str(FRONTEND_DIR / "css"), filename)

    @app.route("/js/<path:filename>", methods=["GET"])
    def serve_js(filename: str) -> Response:
        return send_from_directory(str(FRONTEND_DIR / "js"), filename)

    @app.route("/assets/<path:filename>", methods=["GET"])
    def serve_assets(filename: str) -> Response:
        return send_from_directory(str(FRONTEND_DIR / "assets"), filename)

    # Manejadores de errores HTTP JSON
    @app.errorhandler(404)
    def handle_not_found(error: Any) -> tuple[Response, int]:
        return (
            jsonify(
                {
                    "status": "error",
                    "error_code": "NOT_FOUND",
                    "message": f"Recurso no encontrado: {error}",
                }
            ),
            404,
        )

    @app.errorhandler(405)
    def handle_method_not_allowed(error: Any) -> tuple[Response, int]:
        return (
            jsonify(
                {
                    "status": "error",
                    "error_code": "METHOD_NOT_ALLOWED",
                    "message": f"Método HTTP no permitido para esta ruta: {error}",
                }
            ),
            405,
        )

    return app


app = create_app()


if __name__ == "__main__":
    cfg = load_config()
    app.run(host="0.0.0.0", port=cfg.port, debug=cfg.flask_debug)
