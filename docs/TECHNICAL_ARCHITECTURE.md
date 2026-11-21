# Documentación Técnica de Arquitectura y Evidencias — POETA

Este documento detalla la arquitectura interna, la máquina de estados, el modelo relacional de datos, los protocolos de comunicación con **Ollama Cloud** y **Supabase PostgreSQL**, y la guía de verificación de evidencias de funcionamiento de **POETA: Agente Persistente de Creación Literaria Autónoma**.

---

## 1. Diagrama de Arquitectura del Sistema

```mermaid
flowchart TB
    subgraph Client["Navegador del Usuario (Sin lógica de planificación)"]
        UI["Dashboard Editorial HTML5 / CSS3 / JS<br/>(4 Vistas: Panel, Poema, Evolución, Bitácora)"]
    end

    subgraph RenderWeb["Render Web Service: poeta-api (Gunicorn + Flask)"]
        API["API REST Flask (/api/*)<br/>Validación, Auth X-Admin-Token, Serialización Bogota TZ"]
        CoreWeb["PoetaAgent (Inicio de Ciclo Minuto 00)"]
    end

    subgraph RenderWorker["Render Background Worker: poeta-worker (Proceso Independiente)"]
        Sched["PoetaScheduler (APScheduler BackgroundScheduler)<br/>max_instances=1, coalesce=True"]
        Recov["RecoveryManager<br/>Recuperación tras reinicios sin ráfagas"]
        CoreWorker["PoetaAgent + PoetryEngine + StateManager"]
    end

    subgraph ExternalCloud["Servicios Cloud Externos"]
        Supabase[("Supabase PostgreSQL<br/>Tablas: poems, poem_versions,<br/>agent_logs, agent_state")]
        Ollama["Ollama Cloud REST API<br/>POST https://ollama.com/api/chat<br/>Auth: Bearer OLLAMA_API_KEY"]
    end

    UI <-->|"HTTP JSON (fetch)"| API
    API --> CoreWeb
    CoreWeb <-->|"SQLAlchemy 2.0 (TIMESTAMPTZ UTC)"| Supabase
    CoreWeb <-->|"HTTPS JSON (stream: false)"| Ollama

    Sched --> Recov
    Sched --> CoreWorker
    Recov <-->|"SELECT ... FOR UPDATE"| Supabase
    CoreWorker <-->|"Transacciones Idempotentes"| Supabase
    CoreWorker <-->|"HTTPS JSON (stream: false)"| Ollama
```

---

## 2. Diagrama de Flujo del Agente (Ciclo de 60 Minutos)

```mermaid
flowchart TD
    Start(["Solicitud Inicio de Ciclo (POST /api/agent/start)"]) --> CheckActive{"¿Existe poema activo en Supabase?<br/>(idx_single_active_poem)"}
    CheckActive -- "Sí" --> Reject409["Rechazar con HTTP 409 Conflict<br/>(Exclusividad garantizada)"]
    CheckActive -- "No" --> InitCycle["Crear registro en poems (INITIALIZING)<br/>started_at = T0, scheduled_end_at = T0 + 60m"]
    InitCycle --> Gen1["Minuto 00 (Intervención 1/6):<br/>Solicitar Título, Temática y Estrofa I a Ollama Cloud"]
    Gen1 --> Valid1{"¿Respuesta válida?"}
    Valid1 -- "No" --> AbortInit["Registrar ERROR_DETECTED,<br/>revertir ciclo inicial y volver a IDLE"]
    Valid1 -- "Sí" --> SaveV1["Guardar Versión 1 en poem_versions<br/>next_execution_at = T0 + 10m<br/>Estado -> RUNNING"]

    SaveV1 --> WaitTick["Background Worker: Espera Programada (APScheduler)"]
    WaitTick --> EvalTime{"Evaluar Reloj UTC vs Ciclo"}
    EvalTime -- "now >= scheduled_end_at (Minuto 60)" --> CloseCycle["Finalizar Ciclo:<br/>status = COMPLETED, completed_at = now<br/>Liberar active_poem_id (No iniciar otro poema)"]
    EvalTime -- "completed < 6 y now >= next_execution_at" --> RecoverMem["Recuperar Memoria Persistente desde PostgreSQL:<br/>Título, Temática y Estrofas 1..N confirmadas"]
    RecoverMem --> SendOllama["Enviar Contexto Completo a Ollama Cloud<br/>(Estado -> GENERATING)"]
    SendOllama --> ValidStanza{"¿Estrofa única válida y<br/>sin sustituir el poema?"}
    ValidStanza -- "Error / Inválida" --> HandleErr["Escenarios A/D: Conservar versión N intacta,<br/>no incrementar contador, estado -> ERROR,<br/>programar reintento controlado"]
    HandleErr --> WaitTick
    ValidStanza -- "Válida" --> SaveVN["Unir nueva estrofa al poema acumulado,<br/>guardar Versión N+1 (UNIQUE poem_id, intervention_number),<br/>calcular siguiente ejecución (+10m) y estado -> RUNNING"]
    SaveVN --> WaitTick
```

---

## 3. Diagrama de la Máquina de Estados Controlada

```mermaid
stateDiagram-v2
    [*] --> IDLE
    IDLE --> INITIALIZING : POST /api/agent/start (Sin ciclo activo)
    IDLE --> RECOVERING : Reinicio del Background Worker
    INITIALIZING --> GENERATING : Petición Intervención 1 (Minuto 00)
    INITIALIZING --> ERROR : Fallo inicial de red o validación
    GENERATING --> RUNNING : Versión confirmada y almacenada en Supabase
    GENERATING --> ERROR : Timeout de Ollama Cloud o respuesta defectuosa
    RUNNING --> GENERATING : Minuto 10, 20, 30, 40 o 50 alcanzado
    RUNNING --> RECOVERING : Reinicio del Worker detectado
    RUNNING --> COMPLETED : Minuto 60 alcanzado (Archivado automático)
    RECOVERING --> RUNNING : Memoria sincronizada / Intervención pendiente única ejecutada
    RECOVERING --> COMPLETED : El ciclo ya superó los 60 minutos
    RECOVERING --> IDLE : Sin ciclos activos pendientes
    ERROR --> GENERATING : Reintento controlado
    ERROR --> RECOVERING : Recuperación del Worker
    ERROR --> COMPLETED : Minuto 60 alcanzado (Archiva última versión válida)
    COMPLETED --> INITIALIZING : Usuario inicia manualmente un nuevo ciclo
```

---

## 4. Modelo Entidad-Relación (Supabase PostgreSQL)

```mermaid
erDiagram
    POEMS ||--o{ POEM_VERSIONS : "tiene 1..6 versiones inmutables"
    POEMS ||--o{ AGENT_LOGS : "registra bitácora"
    POEMS |o--o| AGENT_STATE : "referenciado como active_poem_id"

    POEMS {
        uuid id PK
        varchar title
        varchar theme
        text initial_prompt
        varchar status "INITIALIZING | RUNNING | GENERATING | RECOVERING | ERROR | COMPLETED"
        text full_text
        integer total_interventions "Default: 6"
        integer completed_interventions "0..6"
        timestamptz started_at
        timestamptz scheduled_end_at
        timestamptz next_execution_at
        timestamptz completed_at
        timestamptz created_at
        timestamptz updated_at
    }

    POEM_VERSIONS {
        uuid id PK
        uuid poem_id FK "UNIQUE(poem_id, intervention_number)"
        integer intervention_number "1..6"
        text added_stanza
        text full_text
        varchar model_name
        text prompt_snapshot
        timestamptz generated_at
        integer generation_duration_ms
        varchar generation_status "CONFIRMED"
    }

    AGENT_LOGS {
        uuid id PK
        uuid poem_id FK "Nullable"
        varchar event_type
        text description
        varchar status
        jsonb details "Incluye created, scheduled, actual y finished timestamps"
        timestamptz created_at
    }

    AGENT_STATE {
        uuid id PK "Singleton 00000000-0000-0000-0000-000000000001"
        varchar agent_status
        uuid active_poem_id FK "Nullable"
        timestamptz last_execution_at
        timestamptz next_execution_at
        integer last_intervention_number
        timestamptz updated_at
    }
```

---

## 5. Flujo de Comunicación con Ollama Cloud

```mermaid
sequenceDiagram
    autonumber
    participant Worker as PoetaAgent / PoetryEngine
    participant Client as OllamaCloudClient (api/ollama_client.py)
    participant Ollama as Ollama Cloud API (https://ollama.com/api/chat)
    participant DB as Supabase PostgreSQL

    Worker->>DB: Recuperar memoria persistente (título, temática, estrofas 1..N)
    DB-->>Worker: PersistentPoemMemory
    Worker->>DB: Transición a GENERATING + Log OLLAMA_REQUEST_SENT
    Worker->>Client: generate_text(system_prompt, user_prompt_con_memoria)
    Client->>Ollama: POST /api/chat (Authorization: Bearer OLLAMA_API_KEY, stream: false)
    Ollama-->>Client: HTTP 200 JSON {"model": "...", "message": {"content": "..."}, "done": true}
    Client-->>Worker: LLMGenerationResult (content, model_name, prompt_snapshot, duration_ms)
    Worker->>Worker: Validar estructura y verificar que no sustituya el poema (Escenario D)
    Worker->>Worker: Unir nueva estrofa al full_text acumulado
    Worker->>DB: Guardar PoemVersion + Log OLLAMA_RESPONSE_RECEIVED + VERSION_STORED + Estado RUNNING
```

---

## 6. Flujo de Recuperación de Memoria tras Reinicio del Worker

```mermaid
sequenceDiagram
    autonumber
    participant Render as Render Container Runtime
    participant Worker as worker.py / PoetaScheduler
    participant Recov as RecoveryManager (agent/recovery.py)
    participant DB as Supabase PostgreSQL

    Render->>Worker: Inicia proceso (python worker.py)
    Worker->>DB: Registrar evento AGENT_STARTED
    Worker->>Recov: recover_after_restart()
    Recov->>DB: SELECT * FROM poems WHERE status IN (...) FOR UPDATE
    alt Sin poema activo
        DB-->>Recov: None
        Recov->>DB: Asegurar agent_state = IDLE
    else Existe poema activo
        DB-->>Recov: Poema activo encontrado
        Recov->>DB: Transición a RECOVERING + Consultar poem_versions confirmadas
        DB-->>Recov: Versiones 1..K
        Recov->>Recov: Sincronizar completed_interventions = K y full_text
        alt now >= scheduled_end_at (Ya pasaron 60 minutos - Escenario E)
            Recov->>DB: finalize_and_archive_poem() -> Estado COMPLETED (No genera más estrofas)
        else now < scheduled_end_at y now >= next_execution_at (Hubo intervalos apagado - Escenario B)
            Recov->>Worker: Ejecutar COMO MÁXIMO UNA intervención pendiente (#K+1)
            Worker->>DB: Guardar versión #K+1 y recalcular next_execution_at <= scheduled_end_at
        else now < next_execution_at (En horario normal)
            Recov->>DB: Restaurar estado RUNNING y programar trigger en APScheduler
        end
    end
```

---

## 7. Guía de Evidencias de Funcionamiento

A continuación se documentan las estructuras reales verificables que genera el sistema durante su operación para las evidencias académicas:

### 7.1. Ejemplo de Petición Real a Ollama Cloud (`POST https://ollama.com/api/chat`)
```json
{
  "model": "gemma3:27b",
  "messages": [
    {
      "role": "system",
      "content": "Eres POETA, un escritor y poeta literario hispanoamericano de alta sensibilidad estética. Tu misión es componer una obra poética original en español a lo largo de seis intervenciones. Mantienes una voz lírica coherente, imágenes evocadoras, ritmo cuidado y progresión temática. Nunca incluyes saludos, explicaciones técnicas, notas al pie ni comentarios externos a la obra."
    },
    {
      "role": "user",
      "content": "Continuación de creación poética — Intervención 2 de 6.\n\n=== MEMORIA PERSISTENTE DEL POEMA ===\nTítulo: Liturgia del Reloj de Arena\nTemática: La persistencia de la memoria frente al paso inexorable del tiempo.\nEstrofas completadas hasta ahora (1 de 6):\n\n[Estrofa 1]\nCae la arena fina en el cristal dormido,\ncomo un río secreto que numera la sombra,\ny en cada grano leve que se juzga perdido\nlate un eco antiguo que en silencio nos nombra.\n=====================================\n\nDirectriz literaria para esta intervención:\nSegunda estrofa (Minuto 10): expande la imagen inicial y profundiza en el motivo central sin repetir versos previos."
    }
  ],
  "stream": false,
  "options": {
    "temperature": 0.75
  }
}
```

### 7.2. Ejemplo de Respuesta de Ollama Cloud
```json
{
  "model": "gemma3:27b",
  "created_at": "2026-11-21T15:10:02.418Z",
  "message": {
    "role": "assistant",
    "content": "Sobre el muro de cal se alarga la penumbra,\nhilando los minutos con agujas de plata,\nmientras una centella de recuerdos alumbra\nel nudo que la tarde lentamente desata."
  },
  "done": true
}
```

### 7.3. Evolución de Versiones Consecutivas en `GET /api/poem/{id}/versions`
- **Versión 1 (`intervention_number: 1`, Minuto 00)**:
  - `added_stanza`: Estrofa I (4 versos).
  - `full_text`: Estrofa I.
- **Versión 2 (`intervention_number: 2`, Minuto 10)**:
  - `added_stanza`: Estrofa II (4 versos).
  - `full_text`: Estrofa I + `\n\n` + Estrofa II.
- **Versiones 3 a 6 (Minutos 20, 30, 40 y 50)**:
  - Cada versión conserva de manera inmutable el `full_text` acumulado hasta ese instante, la `added_stanza` individual, el `prompt_snapshot` con la memoria enviada y `generation_duration_ms`.

### 7.4. Registro de Actividad y Estado `COMPLETED` al Minuto 60 (`GET /api/agent/logs`)
Cada evento almacenado en `agent_logs` conserva en su columna JSONB `details` las cuatro marcas temporales de auditoría:
- `event_created_at_bogota`: Hora exacta de creación del registro.
- `scheduled_at_bogota`: Hora en que estaba programada la intervención.
- `actual_execution_at_bogota`: Hora real en que el worker inició la ejecución.
- `finished_at_bogota`: Hora de finalización y confirmación en Supabase.
