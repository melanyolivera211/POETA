"""
Pruebas de Memoria Persistente, Idempotencia y Exclusividad en Base de Datos (tests/test_memory.py).

Verifica:
- Almacenamiento y recuperación real de memoria desde la base de datos.
- Almacenamiento inmutable de versiones completas y estrofas individuales.
- Restricción UNIQUE(poem_id, intervention_number) contra intervenciones duplicadas.
- Exclusividad de 1 solo poema activo simultáneamente validada en la base de datos.
- Marcas temporales en UTC y conversión a America/Bogota.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agent.memory import (
    ActiveCycleAlreadyExistsError,
    DuplicateInterventionError,
    PersistentMemoryRepository,
)
from config import to_bogota_iso, utc_now
from database.connection import DatabaseManager


def test_memory_persistence_and_chronological_version_reconstruction(
    db_manager: DatabaseManager,
) -> None:
    repo = PersistentMemoryRepository(db_manager)
    start_time = datetime(2026, 11, 21, 15, 0, 0, tzinfo=timezone.utc)
    end_time = start_time + timedelta(minutes=60)
    next_time = start_time + timedelta(minutes=10)

    with db_manager.session_scope() as session:
        poem = repo.initialize_cycle_record(
            session,
            initial_prompt="Poema sobre la memoria",
            total_interventions=6,
            started_at=start_time,
            scheduled_end_at=end_time,
            next_execution_at=next_time,
        )
        poem_id = poem.id

        repo.save_confirmed_intervention(
            session,
            poem=poem,
            intervention_number=1,
            added_stanza="Estrofa uno versos iniciales\nque abren la memoria.",
            full_text="Estrofa uno versos iniciales\nque abren la memoria.",
            model_name="gemma3:27b",
            prompt_snapshot="Prompt 1",
            generation_duration_ms=210,
            next_execution_at=next_time,
            title="El Archivo del Tiempo",
            theme="La memoria persistente",
        )

        repo.save_confirmed_intervention(
            session,
            poem=poem,
            intervention_number=2,
            added_stanza="Estrofa dos que continúa\nel cauce del poema.",
            full_text=(
                "Estrofa uno versos iniciales\nque abren la memoria.\n\n"
                "Estrofa dos que continúa\nel cauce del poema."
            ),
            model_name="gemma3:27b",
            prompt_snapshot="Prompt 2",
            generation_duration_ms=195,
            next_execution_at=start_time + timedelta(minutes=20),
        )

    # Recuperar desde una nueva sesión limpia para demostrar persistencia real en disco
    with db_manager.session_scope() as session:
        memory = repo.recover_poem_memory(session, poem_id)
        assert memory is not None
        assert memory.title == "El Archivo del Tiempo"
        assert memory.theme == "La memoria persistente"
        assert memory.completed_interventions == 2
        assert memory.last_version_number == 2
        assert len(memory.stanzas) == 2
        assert "Estrofa uno" in memory.stanzas[0]
        assert "Estrofa dos" in memory.stanzas[1]

        versions = repo.get_poem_versions(session, poem_id)
        assert len(versions) == 2
        assert versions[0].intervention_number == 1
        assert versions[1].intervention_number == 2
        assert versions[0].full_text != versions[1].full_text


def test_duplicate_intervention_is_rejected_by_idempotency_and_unique_constraint(
    db_manager: DatabaseManager,
) -> None:
    repo = PersistentMemoryRepository(db_manager)
    now = utc_now()

    with db_manager.session_scope() as session:
        poem = repo.initialize_cycle_record(
            session,
            initial_prompt="Prueba idempotencia",
            total_interventions=6,
            started_at=now,
            scheduled_end_at=now + timedelta(minutes=60),
            next_execution_at=now + timedelta(minutes=10),
        )
        repo.save_confirmed_intervention(
            session,
            poem=poem,
            intervention_number=1,
            added_stanza="Primera estrofa única\ncon dos versos.",
            full_text="Primera estrofa única\ncon dos versos.",
            model_name="gemma3:27b",
            prompt_snapshot="Prompt 1",
            generation_duration_ms=100,
            next_execution_at=now + timedelta(minutes=10),
            title="Poema Único",
            theme="Idempotencia",
        )

        with pytest.raises(DuplicateInterventionError):
            repo.save_confirmed_intervention(
                session,
                poem=poem,
                intervention_number=1,
                added_stanza="Intento duplicado de primera estrofa\nno permitido.",
                full_text="Intento duplicado de primera estrofa\nno permitido.",
                model_name="gemma3:27b",
                prompt_snapshot="Prompt 1 dup",
                generation_duration_ms=100,
                next_execution_at=now + timedelta(minutes=10),
            )


def test_simultaneous_active_cycles_blocked_in_database(
    db_manager: DatabaseManager,
) -> None:
    repo = PersistentMemoryRepository(db_manager)
    now = utc_now()

    with db_manager.session_scope() as session:
        _ = repo.initialize_cycle_record(
            session,
            initial_prompt="Primer ciclo activo",
            total_interventions=6,
            started_at=now,
            scheduled_end_at=now + timedelta(minutes=60),
            next_execution_at=now + timedelta(minutes=10),
        )

    with db_manager.session_scope() as session:
        with pytest.raises(ActiveCycleAlreadyExistsError):
            repo.initialize_cycle_record(
                session,
                initial_prompt="Segundo ciclo simultáneo prohibido",
                total_interventions=6,
                started_at=now,
                scheduled_end_at=now + timedelta(minutes=60),
                next_execution_at=now + timedelta(minutes=10),
            )


def test_utc_to_america_bogota_timestamp_conversion() -> None:
    # 15:00:00 UTC equivale a 10:00:00-05:00 en America/Bogota
    utc_dt = datetime(2026, 11, 21, 15, 0, 0, tzinfo=timezone.utc)
    bogota_iso = to_bogota_iso(utc_dt)
    assert bogota_iso is not None
    assert bogota_iso.startswith("2026-11-21T10:00:00-05:00")
