"""
Configuración central y reglas de negocio inmutables de POETA.

Define las constantes obligatorias del ciclo literario de 60 minutos,
la conexión con Supabase (PostgreSQL), la configuración de Google Gemini AI,
las utilidades de zona horaria (UTC -> America/Bogota) y la protección
del modo acelerado de pruebas.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("poeta.config")

# ==============================================================================
# REGLAS DE NEGOCIO OBLIGATORIAS (SECCIÓN 5)
# ==============================================================================
PRODUCTION_CYCLE_DURATION_MINUTES: int = 60
PRODUCTION_INTERVENTION_INTERVAL_MINUTES: int = 10
PRODUCTION_TOTAL_INTERVENTIONS: int = 6
PRODUCTION_MAX_ACTIVE_POEMS: int = 1
PRODUCTION_FIRST_INTERVENTION_MINUTE: int = 0
PRODUCTION_LAST_INTERVENTION_MINUTE: int = 50
PRODUCTION_CLOSE_MINUTE: int = 60
AUTO_START_NEXT_POEM: bool = False
PERMANENT_PERSISTENCE: bool = True

DEFAULT_DISPLAY_TIMEZONE: str = "America/Bogota"
DEFAULT_GEMINI_BASE_URL: str = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_GEMINI_MODEL: str = "gemini-3.1-flash-lite"


def get_bogota_tz() -> ZoneInfo:
    """Retorna el objeto ZoneInfo para America/Bogota (o la zona configurada)."""
    tz_name = os.getenv("DISPLAY_TIMEZONE", DEFAULT_DISPLAY_TIMEZONE).strip() or DEFAULT_DISPLAY_TIMEZONE
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return ZoneInfo(DEFAULT_DISPLAY_TIMEZONE)


def utc_now() -> datetime:
    """Retorna la fecha y hora actual con zona horaria UTC explícita (TIMESTAMPTZ)."""
    return datetime.now(timezone.utc)


def ensure_utc(dt: datetime | None) -> datetime | None:
    """Garantiza que un objeto datetime tenga zona horaria UTC explícita."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_bogota_iso(dt: datetime | None) -> str | None:
    """Convierte un datetime UTC a formato ISO 8601 en la zona America/Bogota."""
    utc_dt = ensure_utc(dt)
    if utc_dt is None:
        return None
    return utc_dt.astimezone(get_bogota_tz()).isoformat()


def to_utc_iso(dt: datetime | None) -> str | None:
    """Convierte un datetime a formato ISO 8601 en UTC."""
    utc_dt = ensure_utc(dt)
    if utc_dt is None:
        return None
    return utc_dt.isoformat()


def format_bogota_display(dt: datetime | None) -> str | None:
    """Formatea un datetime UTC a cadena legible en America/Bogota."""
    utc_dt = ensure_utc(dt)
    if utc_dt is None:
        return None
    bogota_dt = utc_dt.astimezone(get_bogota_tz())
    return bogota_dt.strftime("%Y-%m-%d %H:%M:%S")


def normalize_database_url(raw_url: str | None, app_env: str = "development") -> str:
    """
    Normaliza la cadena DATABASE_URL para SQLAlchemy 2.0.
    Convierte prefijos `postgres://` (comunes en Render/Supabase) a `postgresql+psycopg2://`.
    """
    if not raw_url or not raw_url.strip():
        if app_env == "testing":
            return "sqlite:///./test_poeta.sqlite3"
        return ""

    url = raw_url.strip()
    if url.startswith("postgres://"):
        url = "postgresql+psycopg2://" + url[len("postgres://") :]
    elif url.startswith("postgresql://"):
        url = "postgresql+psycopg2://" + url[len("postgresql://") :]
    return url


@dataclass(frozen=True)
class AppConfig:
    """Configuración inmutable de la aplicación POETA."""

    app_env: str
    secret_key: str
    admin_api_token: str
    cors_allowed_origins: str
    port: int
    flask_debug: bool

    # Base de datos (Supabase PostgreSQL)
    database_url: str
    supabase_url: str
    supabase_anon_key: str
    supabase_service_role_key: str

    # Proveedor de IA (Google Gemini API)
    ai_provider: str
    gemini_base_url: str
    gemini_api_key: str
    gemini_model: str
    ai_timeout_seconds: int
    ai_max_retries: int

    # Planificación y Reglas de Negocio
    cycle_duration_minutes: int
    intervention_interval_minutes: int
    cycle_duration_seconds: int
    intervention_interval_seconds: int
    total_interventions: int
    max_active_poems: int
    auto_start_next_poem: bool
    display_timezone: str
    worker_poll_seconds: int
    accelerated_test_mode: bool

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def is_testing(self) -> bool:
        return self.app_env == "testing"

    @property
    def has_database_configured(self) -> bool:
        return bool(self.database_url and "tu-proyecto.supabase.co" not in self.database_url)

    @property
    def has_gemini_configured(self) -> bool:
        placeholder_values = {
            "",
            "tu_gemini_api_key",
            "PEGAR_AQUI_TU_GEMINI_API_KEY",
            "tu_api_key_de_gemini",
        }
        return bool(self.gemini_api_key and self.gemini_api_key not in placeholder_values)

    @property
    def active_ai_model(self) -> str:
        return self.gemini_model

    @property
    def admin_auth_required(self) -> bool:
        return bool(
            self.admin_api_token
            and self.admin_api_token != "cambia-este-token-administrativo-seguro"
        )


def load_config(overrides: dict | None = None) -> AppConfig:
    """
    Carga la configuración desde variables de entorno (y overrides opcionales en tests).
    Garantiza que el modo acelerado NUNCA pueda activarse fuera de APP_ENV=testing.
    """
    env = dict(os.environ)
    if overrides:
        for k, v in overrides.items():
            env[k] = str(v)

    app_env = env.get("APP_ENV", "development").strip().lower()
    requested_accelerated = env.get("POETA_ACCELERATED_TEST", "false").strip().lower() == "true"

    if requested_accelerated and app_env != "testing":
        logger.warning(
            "Intento de activar POETA_ACCELERATED_TEST con APP_ENV='%s' rechazado. "
            "Las reglas de 60 minutos / 10 minutos se mantienen estrictamente.",
            app_env,
        )
        accelerated_test_mode = False
    else:
        accelerated_test_mode = requested_accelerated and app_env == "testing"

    if accelerated_test_mode:
        cycle_duration_seconds = int(env.get("TEST_CYCLE_DURATION_SECONDS", "60"))
        intervention_interval_seconds = int(env.get("TEST_INTERVENTION_INTERVAL_SECONDS", "10"))
    else:
        cycle_duration_seconds = PRODUCTION_CYCLE_DURATION_MINUTES * 60
        intervention_interval_seconds = PRODUCTION_INTERVENTION_INTERVAL_MINUTES * 60

    raw_db_url = env.get("DATABASE_URL", "")
    database_url = normalize_database_url(raw_db_url, app_env=app_env)

    gemini_api_key = (
        env.get("GEMINI_API_KEY", "").strip()
        or env.get("GOOGLE_API_KEY", "").strip()
    )

    gemini_base_url = (
        env.get("GEMINI_BASE_URL", DEFAULT_GEMINI_BASE_URL).strip().rstrip("/")
        or DEFAULT_GEMINI_BASE_URL
    )
    gemini_model = (
        env.get("GEMINI_MODEL", DEFAULT_GEMINI_MODEL).strip()
        or DEFAULT_GEMINI_MODEL
    )

    timeout_seconds = int(
        env.get("GEMINI_TIMEOUT_SECONDS")
        or env.get("AI_TIMEOUT_SECONDS")
        or "60"
    )
    max_retries = int(
        env.get("GEMINI_MAX_RETRIES")
        or env.get("AI_MAX_RETRIES")
        or "2"
    )

    return AppConfig(
        app_env=app_env,
        secret_key=env.get("SECRET_KEY", "poeta-dev-secret-key"),
        admin_api_token=env.get("ADMIN_API_TOKEN", "").strip(),
        cors_allowed_origins=env.get("CORS_ALLOWED_ORIGINS", "*").strip(),
        port=int(env.get("PORT", "5000")),
        flask_debug=env.get("FLASK_DEBUG", "false").strip().lower() == "true",
        database_url=database_url,
        supabase_url=env.get("SUPABASE_URL", "").strip(),
        supabase_anon_key=env.get("SUPABASE_ANON_KEY", "").strip(),
        supabase_service_role_key=env.get("SUPABASE_SERVICE_ROLE_KEY", "").strip(),
        ai_provider="gemini",
        gemini_base_url=gemini_base_url,
        gemini_api_key=gemini_api_key,
        gemini_model=gemini_model,
        ai_timeout_seconds=timeout_seconds,
        ai_max_retries=max_retries,
        cycle_duration_minutes=PRODUCTION_CYCLE_DURATION_MINUTES,
        intervention_interval_minutes=PRODUCTION_INTERVENTION_INTERVAL_MINUTES,
        cycle_duration_seconds=cycle_duration_seconds,
        intervention_interval_seconds=intervention_interval_seconds,
        total_interventions=PRODUCTION_TOTAL_INTERVENTIONS,
        max_active_poems=PRODUCTION_MAX_ACTIVE_POEMS,
        auto_start_next_poem=AUTO_START_NEXT_POEM,
        display_timezone=env.get("DISPLAY_TIMEZONE", DEFAULT_DISPLAY_TIMEZONE).strip()
        or DEFAULT_DISPLAY_TIMEZONE,
        worker_poll_seconds=int(env.get("WORKER_POLL_SECONDS", "15")),
        accelerated_test_mode=accelerated_test_mode,
    )
