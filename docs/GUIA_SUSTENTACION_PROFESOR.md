# Guía de Sustentación Académica — POETA: Agente Persistente de Creación Literaria Autónoma

Esta guía resume los 5 puntos técnicos esenciales para explicar y demostrar el funcionamiento de **POETA** ante el profesor, indicando **exactamente en qué archivo y líneas de código** ocurre cada proceso.

---

## 1. ¿Dónde se llama a la API de Inteligencia Artificial (Google Gemini) y qué endpoint usa?

- **Archivo**: `api/ollama_client.py` (Clase `GeminiCloudClient`, líneas 112–342).
- **Librería que usa**: `requests` (`requests.Session().post(...)` con JSON sincrónico).
- **Endpoint exacto donde hace la petición**:
  ```text
  POST https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite:generateContent
  ```
- **Cómo se autentica**: Mediante la cabecera HTTP `x-goog-api-key: <GEMINI_API_KEY>` (leída desde `.env`).

### Fragmento de código (`api/ollama_client.py`, líneas 144–205):
```python
    def _build_headers(self) -> dict[str, str]:
        return {
            "x-goog-api-key": self.api_key,
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
        headers = self._build_headers()

        payload: dict[str, Any] = {
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": user_prompt.strip()}],
                }
            ],
            "generationConfig": {"temperature": temperature},
            "systemInstruction": {"parts": [{"text": system_prompt.strip()}]},
        }

        endpoint = f"{self.base_url}/models/{model_name}:generateContent"
        response = self.session.post(
            endpoint,
            headers=headers,
            json=payload,
            timeout=self.timeout_seconds,
        )
```

---

## 2. ¿Cómo se comunican el Frontend (Navegador) y el Backend (API REST Propia en Flask)?

El navegador **nunca** llama directamente a Gemini ni a Supabase, ni ejecuta temporizadores del poema. Todo pasa por la **API REST propia** construida en Flask:

- **Dónde están definidos los 7 endpoints del Backend**: `api/routes.py` (líneas 75–385).
  1. `POST /api/agent/start` -> Inicia el ciclo de 60 minutos y genera la Estrofa 1.
  2. `GET /api/agent/status` -> Devuelve estado (`IDLE`, `RUNNING`, `GENERATING`, `COMPLETED`), progreso y tiempos.
  3. `GET /api/poem/current` -> Devuelve el poema actual y sus estrofas desde Supabase.
  4. `GET /api/poem/history` -> Devuelve poemas archivados (`COMPLETED`).
  5. `GET /api/poem/<id>/versions` -> Devuelve las 6 versiones inmutables para comparar la evolución.
  6. `GET /api/agent/logs` -> Devuelve la bitácora cronológica de eventos (`agent_logs`).
  7. `GET /api/agent/health` -> Verifica que Flask, Supabase y Gemini AI estén activos.

- **Dónde llama el Frontend a esos endpoints**: `frontend/js/dashboard.js` (líneas 83–185), usando `fetch()` con JSON:
```javascript
  async function fetchJson(url, options = {}) {
    const response = await fetch(url, {
      headers: { Accept: "application/json", ...(options.headers || {}) },
      ...options,
    });
    const data = await response.json().catch(() => ({}));
    return { ok: response.ok, status: response.status, data };
  }
```

---

## 3. ¿Dónde se guarda en la Base de Datos y cómo funciona la Memoria Persistente en Supabase?

Toda la persistencia ocurre en **Supabase (PostgreSQL)** mediante **SQLAlchemy 2.0**:

1. **Conexión a Supabase**: `database/connection.py` (líneas 30–115). Usa la variable `DATABASE_URL` hacia el Pooler de Supabase (`aws-0-us-west-2.pooler.supabase.com:5432`) con transacciones atómicas (`session_scope()`).
2. **Las 4 Tablas en PostgreSQL**: `database/models.py` (líneas 54–241) y `database/migrations/001_initial_poeta_schema.sql`:
   - `poems`: Guarda el poema maestro (`id`, `title`, `theme`, `status`, `full_text`, `completed_interventions`, `started_at`, `scheduled_end_at`, `next_execution_at`).
   - `poem_versions`: Guarda una fila inmutable por cada intervención (1 a 6) con `added_stanza` (la estrofa nueva), `full_text` (el poema acumulado hasta ese minuto) y `prompt_snapshot` (la memoria exacta enviada a la IA).
   - `agent_logs`: Bitácora de auditoría con columna `details` en `JSONB`.
   - `agent_state`: Estado global de la máquina de estados del agente.

3. **Dónde se recupera la memoria antes de generar la siguiente estrofa**:
   En `agent/memory.py` -> método `recover_poem_memory()` (líneas 92–170). Antes de cada intervención (Minutos 10, 20, 30, 40 y 50), el agente hace un `SELECT` en `poem_versions` ordenado por `intervention_number ASC`, reconstruye todas las estrofas anteriores desde PostgreSQL y se las pasa a `PoetryEngine.generate_continuation_stanza()` (`agent/poetry_engine.py`, líneas 113–195).

### Fragmento de Recuperación y Guardado de Memoria (`agent/memory.py`, líneas 111–125 y 299–322):
```python
        # 1. RECUPERACIÓN REAL DESDE POSTGRESQL (agent/memory.py, líneas 111-125)
        versions_stmt = (
            select(PoemVersion)
            .where(
                PoemVersion.poem_id == poem.id,
                PoemVersion.generation_status == "CONFIRMED",
            )
            .order_by(PoemVersion.intervention_number.asc())
        )
        versions = list(session.execute(versions_stmt).scalars().all())
        stanzas = [v.added_stanza.strip() for v in versions if v.added_stanza]

        # 2. GUARDADO INMUTABLE DE CADA VERSIÓN (agent/memory.py, líneas 299-322)
        version = PoemVersion(
            id=uuid.uuid4(),
            poem_id=poem.id,
            intervention_number=intervention_number,
            added_stanza=added_stanza,
            full_text=full_text,
            model_name=model_name,
            prompt_snapshot=prompt_snapshot,
            generated_at=now,
            generation_duration_ms=generation_duration_ms,
            generation_status="CONFIRMED",
        )
        session.add(version)
        poem.full_text = full_text
        poem.completed_interventions = intervention_number
        poem.next_execution_at = ensure_utc(next_execution_at)
```

---

## 4. ¿Dónde están los Triggers, Índices y Restricciones que protegen la Base de Datos?

En `database/models.py` (líneas 111–167) y en `database/migrations/001_initial_poeta_schema.sql` (líneas 35–65) existen tres mecanismos críticos a nivel de motor PostgreSQL:

1. **Índice Único Parcial (`idx_single_active_poem`) — Impide 2 poemas activos al mismo tiempo**:
   ```sql
   CREATE UNIQUE INDEX IF NOT EXISTS idx_single_active_poem
       ON public.poems ((1))
       WHERE status IN ('INITIALIZING', 'RUNNING', 'GENERATING', 'RECOVERING', 'ERROR');
   ```
2. **Restricción Única (`uq_poem_versions_poem_intervention`) — Impide estrofas duplicadas**:
   ```sql
   CONSTRAINT uq_poem_versions_poem_intervention UNIQUE (poem_id, intervention_number)
   ```
3. **Bloqueo Transaccional (`SELECT ... FOR UPDATE`)**:
   En `agent/state_manager.py` (líneas 94–97) y `agent/memory.py` (líneas 88–89), se usa `.with_for_update()` para serializar accesos concurrentes entre el servidor web y el worker.

---

## 5. ¿Dónde están los Triggers de Tiempo (Cada 10 Minutos y Cierre a los 60 Minutos)?

El agente autónomo corre en un proceso independiente (`worker.py`) que utiliza **`APScheduler` (`BackgroundScheduler`)** configurado en `agent/scheduler.py` (líneas 33–179):

- Utiliza tres *triggers* de `APScheduler`:
  1. **`IntervalTrigger` (`poeta_cycle_supervisor_tick`)**: Revisa periódicamente el estado del ciclo en Supabase.
  2. **`DateTrigger` (`poeta_next_intervention_trigger`)**: Se programa exactamente en la fecha/hora `next_execution_at` (`started_at + 10 min`, `+ 20 min`, `+ 30 min`, `+ 40 min`, `+ 50 min`) para disparar la generación de las estrofas 2 a 6.
  3. **`DateTrigger` (`poeta_cycle_close_trigger`)**: Se programa exactamente en `scheduled_end_at` (`started_at + 60 min`) para cerrar el ciclo, cambiar el estado a `COMPLETED`, archivar el poema y cancelar los triggers sin generar una séptima estrofa.

### Fragmento de los Triggers en `agent/scheduler.py` (líneas 138–160):
```python
            # Trigger exacto para la siguiente intervención (Minutos 10, 20, 30, 40, 50)
            if next_exec is not None and completed < total:
                run_date = max(now, next_exec)
                self.scheduler.add_job(
                    func=self.run_supervisor_cycle,
                    trigger=DateTrigger(run_date=run_date, timezone=timezone.utc),
                    id=self.INTERVENTION_JOB_ID,
                    name=f"Intervención #{completed + 1} del poema {poem_id_str}",
                    replace_existing=True,
                )

            # Trigger exacto para el cierre automático al Minuto 60
            if scheduled_end is not None:
                close_date = max(now, scheduled_end)
                self.scheduler.add_job(
                    func=self.run_supervisor_cycle,
                    trigger=DateTrigger(run_date=close_date, timezone=timezone.utc),
                    id=self.CLOSE_JOB_ID,
                    name=f"Cierre automático minuto 60 del poema {poem_id_str}",
                    replace_existing=True,
                )
```
