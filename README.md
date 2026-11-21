# POETA — Agente Persistente de Creación Literaria Autónoma

**POETA** es un sistema de software modular en Python que implementa un agente inteligente persistente y autónomo capaz de crear y desarrollar progresivamente una composición poética en español a lo largo de un ciclo regulado de **60 minutos** (6 intervenciones espaciadas cada **10 minutos**), utilizando **Ollama Cloud** como motor de lenguaje, **Supabase (PostgreSQL)** como memoria relacional permanente y un **Dashboard Web Editorial** servido mediante **Flask**.

---

## 1. Problema que Resuelve

Las aplicaciones convencionales basadas en modelos de lenguaje operan de forma reactiva y efímera: dependen de que el usuario mantenga abierta una sesión en el navegador y suelen conservar el contexto únicamente en memoria volátil (RAM o `localStorage`). Si el proceso se reinicia o el navegador se cierra, el flujo de trabajo se interrumpe y el contexto se pierde.

**POETA** resuelve este problema demostrando una arquitectura de **agente autónomo persistente**:
1. **Independencia del navegador**: un proceso *Background Worker* independiente ejecuta las tareas programadas mediante `APScheduler` aunque ningún usuario esté conectado al dashboard.
2. **Memoria persistente real**: antes de cada intervención, el agente recupera desde PostgreSQL el título, la temática y todas las versiones previas confirmadas para construir el contexto literario completo.
3. **Resiliencia e idempotencia**: ante caídas de red o reinicios del contenedor en Render, el agente recupera su estado desde Supabase, evita duplicar intervenciones mediante restricciones `UNIQUE(poem_id, intervention_number)` y retoma el ciclo sin ejecutar ráfagas descontroladas ni extender artificialmente los 60 minutos.

---

## 2. Objetivos

### Objetivo General
Desarrollar un agente inteligente persistente capaz de ejecutar una tarea creativa de forma autónoma y programada, conservando su memoria y contexto entre ejecuciones, mediante integración con un modelo de lenguaje en la nube, almacenamiento permanente en PostgreSQL y una interfaz web editorial de monitoreo.

### Objetivos Específicos
- Implementar comunicación desacoplada con **Ollama Cloud** mediante su API REST oficial (`https://ollama.com/api`).
- Desarrollar un motor de generación y continuación literaria con validación estructural y protección contra sustitución accidental del poema.
- Implementar memoria persistente e inmutable de versiones mediante **Supabase (PostgreSQL)** y **SQLAlchemy 2.0**.
- Programar intervenciones automáticas cada 10 minutos (Minutos `00`, `10`, `20`, `30`, `40` y `50`) y cierre automático al minuto `60`.
- Garantizar la exclusividad de un único poema activo simultáneamente tanto en la capa de negocio como en el motor PostgreSQL.
- Implementar recuperación controlada del estado del agente tras reinicios del proceso.
- Registrar todas las decisiones, transiciones y llamadas con marcas temporales en `UTC` (`TIMESTAMPTZ`) y presentación en `America/Bogota`.
- Exponer una API REST propia en Flask y un Dashboard HTML5/CSS3/JS editorial de cuatro vistas.

---

## 3. Reglas de Negocio Inmutables

| Parámetro | Valor Obligatorio |
|---|---|
| Duración total del ciclo | **60 minutos** |
| Intervalo entre intervenciones | **10 minutos** |
| Cantidad total de intervenciones | **6** (Minutos 00, 10, 20, 30, 40 y 50) |
| Cierre y archivado automático | **Minuto 60** (sin generar una 7.ª estrofa) |
| Poemas activos simultáneamente | **1** (exclusividad validada en código y BD) |
| Inicio del siguiente poema | **No automático** |
| Persistencia de versiones y logs | **Permanente en Supabase PostgreSQL** |

---

## 4. Arquitectura y Tecnologías

- **Backend Web (`poeta-api`)**: Python 3.11+, Flask 3, Flask-CORS, Gunicorn.
- **Agente Autónomo (`poeta-worker`)**: Proceso Python independiente con `APScheduler` (`BackgroundScheduler` con control de concurrencia `max_instances=1` y `coalesce=True`).
- **Proveedor de IA**: Cliente HTTP desacoplado (`LLMProvider` -> `OllamaCloudClient`) conectado a **Ollama Cloud** (`https://ollama.com/api/chat` con `"stream": false`).
- **Base de Datos**: **Supabase (PostgreSQL)**, SQLAlchemy 2.0, migraciones versionadas en SQL (`database/migrations/001_initial_poeta_schema.sql`) y Alembic (`alembic.ini`).
- **Frontend**: HTML5 semántico, CSS3 con diseño editorial contemporáneo (*Swiss Modernism / Literary Journal*), JavaScript moderno (`fetch`, actualización controlada y accesible).

Consulte los diagramas detallados (Arquitectura, Flujo del Agente, Máquina de Estados, Modelo Entidad-Relación y Secuencias de Memoria) en [docs/TECHNICAL_ARCHITECTURE.md](docs/TECHNICAL_ARCHITECTURE.md).

---

## 5. Estructura del Proyecto

```text
POETA/
├── app.py                          # Servidor HTTP Flask (API REST + Dashboard)
├── worker.py                       # Proceso autónomo independiente (APScheduler)
├── config.py                       # Configuración, reglas de negocio y zonas horarias
├── requirements.txt                # Dependencias de Python
├── render.yaml                     # Blueprint de despliegue para Render (Web + Worker)
├── alembic.ini                     # Configuración de migraciones Alembic
├── .env.example                    # Plantilla de variables de entorno (sin secretos)
├── .gitignore                      # Exclusión de .env, cachés y bases locales
├── agent/
│   ├── core.py                     # Orquestador principal PoetaAgent
│   ├── memory.py                   # Repositorio de memoria persistente e idempotencia
│   ├── poetry_engine.py            # Motor literario y validador de estrofas
│   ├── recovery.py                 # Protocolo de recuperación tras interrupciones
│   ├── scheduler.py                # Planificador persistente APScheduler
│   └── state_manager.py            # Máquina de estados controlada (7 estados)
├── api/
│   ├── ollama_client.py            # Integración HTTP con Ollama Cloud REST API
│   ├── routes.py                   # Endpoints de la API REST (/api/...)
│   └── schemas.py                  # Validación de entradas y serialización JSON
├── database/
│   ├── connection.py               # Gestión de motor SQLAlchemy y sesiones
│   ├── models.py                   # Modelos: Poem, PoemVersion, AgentLog, AgentState
│   └── migrations/
│       ├── 001_initial_poeta_schema.sql
│       ├── env.py
│       └── versions/
│           └── 001_initial_poeta_schema.py
├── frontend/
│   ├── index.html                  # Dashboard editorial con las 4 vistas obligatorias
│   ├── css/styles.css              # Estilos tipográficos y responsive
│   ├── js/dashboard.js             # Cliente API, comparador de versiones y timeline
│   └── assets/poeta-monogram.svg   # Monograma vectorial de POETA
├── docs/
│   └── TECHNICAL_ARCHITECTURE.md   # Diagramas técnicos y guía de evidencias
└── tests/
    ├── conftest.py                 # Fixtures y proveedor determinista de pruebas
    ├── test_agent.py               # Pruebas del ciclo completo de 60 minutos
    ├── test_api.py                 # Pruebas de endpoints REST y seguridad
    ├── test_memory.py              # Pruebas de persistencia, UNIQUE y exclusividad
    ├── test_poetry.py              # Pruebas del motor literario y cliente Ollama Cloud
    ├── test_recovery.py            # Pruebas de recuperación tras reinicios
    └── test_scheduler.py           # Pruebas de APScheduler y bloqueo de modo acelerado
```

---

## 6. Configuración Paso a Paso

### 6.1. Requisitos Previos
- **Python 3.11 o superior** (verificado en Python 3.13).
- Una cuenta y proyecto en **Supabase** ([https://supabase.com](https://supabase.com)).
- Una cuenta y clave de API en **Ollama Cloud** ([https://ollama.com/settings/keys](https://ollama.com/settings/keys)).

### 6.2. Instalación Local

```bash
# 1. Clonar el repositorio e ingresar al directorio
git clone <URL_DEL_REPOSITORIO>
cd "Agente Persistente"

# 2. (Opcional) Crear y activar un entorno virtual
python -m venv .venv
# En Windows PowerShell:
.\.venv\Scripts\Activate.ps1
# En Linux/macOS:
# source .venv/bin/activate

# 3. Instalar dependencias
pip install -r requirements.txt

# 4. Crear archivo .env a partir de la plantilla
cp .env.example .env
```

### 6.3. Configuración de Ollama Cloud
1. Inicie sesión en [https://ollama.com](https://ollama.com) y diríjase a **Settings -> Keys** ([https://ollama.com/settings/keys](https://ollama.com/settings/keys)).
2. Genere una nueva API Key.
3. Configure en su archivo `.env` (o en las variables de entorno de Render):
   ```env
   OLLAMA_BASE_URL=https://ollama.com/api
   OLLAMA_API_KEY=su_clave_real_de_ollama_cloud
   OLLAMA_MODEL=gemma3:27b
   ```
   *(Nota: No confunda Ollama Cloud `https://ollama.com/api` con un servidor Ollama local `http://localhost:11434`. En Render no existe un servidor Ollama local preinstalado; todas las peticiones se autentican mediante `Authorization: Bearer <OLLAMA_API_KEY>` contra el endpoint cloud oficial).*

### 6.4. Configuración de Supabase (PostgreSQL)
1. Cree un nuevo proyecto en [Supabase](https://supabase.com/dashboard).
2. En el panel lateral de Supabase, abra **SQL Editor**, copie el contenido íntegro de [`database/migrations/001_initial_poeta_schema.sql`](database/migrations/001_initial_poeta_schema.sql) y ejecútelo (**Run**). Alternativamente, puede aplicar la migración desde terminal con Alembic:
   ```bash
   alembic upgrade head
   ```
3. En Supabase, vaya a **Project Settings -> Database -> Connection string -> URI** y copie la cadena de conexión PostgreSQL (modo *Session Pooler* o conexión directa en puerto `5432`).
4. Configure en `.env`:
   ```env
   DATABASE_URL=postgresql://postgres.[PROJECT-REF]:[PASSWORD]@aws-0-[REGION].pooler.supabase.com:5432/postgres
   SUPABASE_URL=https://[PROJECT-REF].supabase.co
   SUPABASE_ANON_KEY=su_anon_key
   SUPABASE_SERVICE_ROLE_KEY=su_service_role_key_privada
   ```
   > **Seguridad**: `SUPABASE_SERVICE_ROLE_KEY` y `OLLAMA_API_KEY` se utilizan exclusivamente en el backend/worker y jamás se envían al navegador.

---

## 7. Ejecución Local

El sistema consta de dos procesos independientes que deben ejecutarse en terminales separadas:

### Terminal 1: Servidor Web y API REST (`app.py`)
```bash
python app.py
```
El dashboard editorial estará disponible en `http://localhost:5000`.

### Terminal 2: Agente Autónomo Background Worker (`worker.py`)
```bash
python worker.py
```
El worker verificará inmediatamente si existe algún ciclo activo en Supabase (`recover_on_startup`) y ejecutará las intervenciones programadas cada 10 minutos independientemente de que el navegador esté abierto o cerrado.

---

## 8. Ejecución de Pruebas

Para ejecutar la suite completa de pruebas unitarias, de integración y de recuperación:

```bash
python -m pytest -v
```

### Modo de Pruebas Acelerado
Para comprobar rápidamente las 6 intervenciones y el cierre del ciclo (por ejemplo, con intervalos de 1 a 10 segundos), existe un modo acelerado exclusivo para pruebas (`APP_ENV=testing` y `POETA_ACCELERATED_TEST=true`).
Por seguridad, `config.py` bloquea automáticamente cualquier intento de activar `POETA_ACCELERATED_TEST` cuando `APP_ENV` es `production` o `development`.

---

## 9. Referencia de Endpoints de la API REST

| Método | Ruta | Descripción | Códigos HTTP |
|---|---|---|---|
| `POST` | `/api/agent/start` | Inicia un nuevo ciclo de 60 min y genera la 1.ª estrofa. Requiere `X-Admin-Token` si `ADMIN_API_TOKEN` está configurado. | `201 Created`, `401 Unauthorized`, `409 Conflict`, `502/503` |
| `GET` | `/api/agent/status` | Retorna estado de la máquina de estados, poema activo, intervención actual (`X/6`), tiempo transcurrido/restante y próximas ejecuciones en `UTC` y `America/Bogota`. | `200 OK`, `503` |
| `GET` | `/api/poem/current` | Retorna la composición activa (o la última finalizada) con sus estrofas y versiones. | `200 OK`, `503` |
| `GET` | `/api/poem/history` | Lista paginada (`?page=1&per_page=10`) de poemas finalizados (`COMPLETED`). | `200 OK`, `400` |
| `GET` | `/api/poem/{id}/versions` | Retorna las versiones inmutables (`1..6`) de un poema específico. | `200 OK`, `400`, `404 Not Found` |
| `GET` | `/api/agent/logs` | Historial cronológico paginado (`?page=1&per_page=25&event_type=...`) desde `agent_logs`. | `200 OK`, `400` |
| `GET` | `/api/agent/health` | Verifica disponibilidad real de Flask, Supabase PostgreSQL y Ollama Cloud. | `200 OK`, `503 Service Unavailable` |

---

## 10. Despliegue en Render y Límites de Planes

El proyecto incluye el archivo [`render.yaml`](render.yaml) preparado para despliegue mediante **Render Blueprints** conectado a GitHub, definiendo dos servicios separados:

1. **`poeta-api` (Render Web Service)**:
   - Comando de inicio: `gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --threads 4 --timeout 120`
   - Sirve la API REST y el Dashboard HTML.
2. **`poeta-worker` (Render Background Worker)**:
   - Comando de inicio: `python worker.py`
   - Ejecuta `APScheduler` en un proceso único e independiente (nunca se inicia `APScheduler` dentro de las instancias de Gunicorn del Web Service para evitar condiciones de carrera).

### Consideraciones Reales sobre Planes y Costos en Render y Supabase
- **Render Background Worker (`poeta-worker`)**: Render **no ofrece un plan gratuito (Free Tier) para Background Workers**. Para desplegar `poeta-worker` como servicio `worker` continuo en Render se requiere como mínimo el plan **Starter** (\$7 USD/mes).
- **Render Web Service (`poeta-api`)**: Puede desplegarse en el plan **Free** (\$0 USD/mes, aunque Render suspende la instancia tras 15 minutos sin tráfico HTTP y tarda ~30-50 segundos en despertar) o en el plan **Starter** (\$7 USD/mes, siempre activo).
- **Supabase PostgreSQL**: El plan **Free** de Supabase incluye 500 MB de base de datos PostgreSQL y soporte completo para `TIMESTAMPTZ`, `UUID`, `JSONB` y políticas RLS, suficiente para operación académica; los proyectos gratuitos en Supabase se pausan tras 7 días sin actividad de base de datos.
