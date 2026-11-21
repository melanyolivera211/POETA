-- ==============================================================================
-- POETA: Agente Persistente de Creación Literaria Autónoma
-- Migración Versionada 001: Esquema Relacional para Supabase (PostgreSQL)
-- ==============================================================================
-- Ejecutar en el SQL Editor de Supabase o mediante Alembic.
-- Todas las marcas temporales utilizan TIMESTAMPTZ (almacenadas en UTC).
-- ==============================================================================

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- ------------------------------------------------------------------------------
-- 1. TABLA: poems
-- ------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.poems (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title VARCHAR(255) NOT NULL,
    theme VARCHAR(500) NOT NULL,
    initial_prompt TEXT NOT NULL,
    status VARCHAR(50) NOT NULL,
    full_text TEXT NOT NULL DEFAULT '',
    total_interventions INTEGER NOT NULL DEFAULT 6,
    completed_interventions INTEGER NOT NULL DEFAULT 0,
    started_at TIMESTAMPTZ NOT NULL,
    scheduled_end_at TIMESTAMPTZ NOT NULL,
    next_execution_at TIMESTAMPTZ NULL,
    completed_at TIMESTAMPTZ NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_poems_interventions_range
        CHECK (completed_interventions >= 0 AND completed_interventions <= total_interventions)
);

CREATE INDEX IF NOT EXISTS idx_poems_status ON public.poems (status);
CREATE INDEX IF NOT EXISTS idx_poems_next_execution_at ON public.poems (next_execution_at);
CREATE INDEX IF NOT EXISTS idx_poems_created_at ON public.poems (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_poems_status_started_at ON public.poems (status, started_at);

-- Garantía de exclusividad en el motor PostgreSQL:
-- Impide que existan dos poemas activos simultáneamente ante condiciones de carrera.
CREATE UNIQUE INDEX IF NOT EXISTS idx_single_active_poem
    ON public.poems ((1))
    WHERE status IN ('INITIALIZING', 'RUNNING', 'GENERATING', 'RECOVERING', 'ERROR');

-- ------------------------------------------------------------------------------
-- 2. TABLA: poem_versions
-- ------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.poem_versions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    poem_id UUID NOT NULL REFERENCES public.poems(id) ON DELETE CASCADE,
    intervention_number INTEGER NOT NULL,
    added_stanza TEXT NOT NULL,
    full_text TEXT NOT NULL,
    model_name VARCHAR(120) NOT NULL,
    prompt_snapshot TEXT NOT NULL,
    generated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    generation_duration_ms INTEGER NOT NULL DEFAULT 0,
    generation_status VARCHAR(50) NOT NULL DEFAULT 'CONFIRMED',
    CONSTRAINT uq_poem_versions_poem_intervention UNIQUE (poem_id, intervention_number),
    CONSTRAINT chk_poem_versions_intervention_num CHECK (intervention_number >= 1 AND intervention_number <= 6)
);

CREATE INDEX IF NOT EXISTS idx_poem_versions_poem_id ON public.poem_versions (poem_id);
CREATE INDEX IF NOT EXISTS idx_poem_versions_generated_at ON public.poem_versions (generated_at);
CREATE INDEX IF NOT EXISTS idx_poem_versions_poem_num ON public.poem_versions (poem_id, intervention_number);

-- ------------------------------------------------------------------------------
-- 3. TABLA: agent_logs
-- ------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.agent_logs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    poem_id UUID NULL REFERENCES public.poems(id) ON DELETE SET NULL,
    event_type VARCHAR(80) NOT NULL,
    description TEXT NOT NULL,
    status VARCHAR(50) NOT NULL,
    details JSONB NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_agent_logs_poem_id ON public.agent_logs (poem_id);
CREATE INDEX IF NOT EXISTS idx_agent_logs_event_type ON public.agent_logs (event_type);
CREATE INDEX IF NOT EXISTS idx_agent_logs_status ON public.agent_logs (status);
CREATE INDEX IF NOT EXISTS idx_agent_logs_created_at ON public.agent_logs (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_logs_poem_created ON public.agent_logs (poem_id, created_at);

-- ------------------------------------------------------------------------------
-- 4. TABLA: agent_state
-- ------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.agent_state (
    id UUID PRIMARY KEY DEFAULT '00000000-0000-0000-0000-000000000001'::uuid,
    agent_status VARCHAR(50) NOT NULL DEFAULT 'IDLE',
    active_poem_id UUID NULL REFERENCES public.poems(id) ON DELETE SET NULL,
    last_execution_at TIMESTAMPTZ NULL,
    next_execution_at TIMESTAMPTZ NULL,
    last_intervention_number INTEGER NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Insertar estado inicial singleton si no existe
INSERT INTO public.agent_state (
    id, agent_status, active_poem_id, last_execution_at, next_execution_at, last_intervention_number, updated_at
) VALUES (
    '00000000-0000-0000-0000-000000000001'::uuid,
    'IDLE',
    NULL,
    NULL,
    NULL,
    0,
    NOW()
) ON CONFLICT (id) DO NOTHING;

-- ------------------------------------------------------------------------------
-- 5. SEGURIDAD: ROW LEVEL SECURITY (RLS) EN SUPABASE
-- ------------------------------------------------------------------------------
ALTER TABLE public.poems ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.poem_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.agent_logs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.agent_state ENABLE ROW LEVEL SECURITY;

-- Políticas de solo lectura para consultas públicas si se habilita Realtime desde cliente
DROP POLICY IF EXISTS "public_read_poems" ON public.poems;
CREATE POLICY "public_read_poems" ON public.poems FOR SELECT USING (true);

DROP POLICY IF EXISTS "public_read_poem_versions" ON public.poem_versions;
CREATE POLICY "public_read_poem_versions" ON public.poem_versions FOR SELECT USING (true);

DROP POLICY IF EXISTS "public_read_agent_logs" ON public.agent_logs;
CREATE POLICY "public_read_agent_logs" ON public.agent_logs FOR SELECT USING (true);

DROP POLICY IF EXISTS "public_read_agent_state" ON public.agent_state;
CREATE POLICY "public_read_agent_state" ON public.agent_state FOR SELECT USING (true);
-- Las operaciones INSERT / UPDATE / DELETE quedan restringidas exclusivamente
-- al backend Flask y al Background Worker (conexión PostgreSQL / service_role).
