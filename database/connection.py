"""
Gestión de conexiones y sesiones SQLAlchemy hacia Supabase PostgreSQL (database/connection.py).

No simula conexiones exitosas si `DATABASE_URL` no está configurada o si el servidor
PostgreSQL no responde. Proporciona pooling con `pool_pre_ping=True` para tolerar
desconexiones transitorias en entornos cloud (Render + Supabase Pooler).
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from typing import Any, Generator

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from config import AppConfig, load_config
from database.models import Base

logger = logging.getLogger("poeta.database")


class DatabaseNotConfiguredError(RuntimeError):
    """Excepción lanzada cuando DATABASE_URL no ha sido configurada."""


class DatabaseManager:
    """Administrador de motor SQLAlchemy, sesiones transaccionales y health checks."""

    def __init__(self, config: AppConfig | None = None, engine: Engine | None = None) -> None:
        self.config = config or load_config()
        self._engine: Engine | None = engine
        self._session_factory: sessionmaker[Session] | None = None
        if self._engine is not None:
            self._session_factory = sessionmaker(
                bind=self._engine,
                autoflush=False,
                autocommit=False,
                expire_on_commit=False,
            )

    @property
    def engine(self) -> Engine:
        if self._engine is None:
            if not self.config.database_url:
                raise DatabaseNotConfiguredError(
                    "DATABASE_URL no está configurada. Configure la cadena de conexión PostgreSQL "
                    "de Supabase en las variables de entorno."
                )
            connect_args: dict[str, Any] = {}
            engine_kwargs: dict[str, Any] = {
                "pool_pre_ping": True,
                "future": True,
            }

            if self.config.database_url.startswith("sqlite"):
                connect_args["check_same_thread"] = False
            else:
                engine_kwargs["pool_size"] = 5
                engine_kwargs["max_overflow"] = 10
                engine_kwargs["pool_recycle"] = 300
                connect_args["connect_timeout"] = 10

            self._engine = create_engine(
                self.config.database_url,
                connect_args=connect_args,
                **engine_kwargs,
            )

            if self.config.database_url.startswith("sqlite"):
                @event.listens_for(self._engine, "connect")
                def _set_sqlite_pragma(dbapi_connection: Any, _connection_record: Any) -> None:
                    cursor = dbapi_connection.cursor()
                    cursor.execute("PRAGMA foreign_keys=ON")
                    cursor.close()

            self._session_factory = sessionmaker(
                bind=self._engine,
                autoflush=False,
                autocommit=False,
                expire_on_commit=False,
            )
        return self._engine

    def create_tables(self) -> None:
        """Crea las tablas del esquema relacional si no existen."""
        Base.metadata.create_all(bind=self.engine)

    @contextmanager
    def session_scope(self) -> Generator[Session, None, None]:
        """Context manager transaccional que confirma o revierte automáticamente."""
        _ = self.engine
        assert self._session_factory is not None
        session: Session = self._session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def health_check(self) -> dict[str, Any]:
        """
        Ejecuta una consulta real `SELECT 1` contra la base de datos configurada.
        Nunca finge éxito si falta la configuración o falla la conexión.
        """
        if not self.config.has_database_configured and not self.config.is_testing:
            return {
                "provider": "supabase_postgresql",
                "configured": False,
                "available": False,
                "message": "DATABASE_URL de Supabase no configurada en el entorno.",
            }

        start = time.perf_counter()
        try:
            with self.engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            latency_ms = int((time.perf_counter() - start) * 1000)
            dialect = self.engine.dialect.name
            return {
                "provider": "supabase_postgresql" if dialect == "postgresql" else dialect,
                "configured": True,
                "available": True,
                "dialect": dialect,
                "latency_ms": latency_ms,
                "message": "Conexión verificada con la base de datos.",
            }
        except Exception as exc:
            latency_ms = int((time.perf_counter() - start) * 1000)
            return {
                "provider": "supabase_postgresql",
                "configured": bool(self.config.database_url),
                "available": False,
                "latency_ms": latency_ms,
                "message": f"Error de conexión con la base de datos: {exc}",
            }
