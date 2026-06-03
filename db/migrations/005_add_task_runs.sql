-- Migration 005: task_runs — per-task provenance for reflect_task.py runs.
--
-- Creates task_runs to record one row per reflect_task.py --store invocation.
-- Each row captures scope, description, files, tests, outcome, notes, lesson count.
--
-- FK is on memory_signals (not memory_atoms): signals are immutable provenance
-- records. An atom can be reinforced by signals from multiple later task runs.
--
-- Apply to existing databases:
--   psql $DATABASE_URL -f db/migrations/005_add_task_runs.sql

CREATE TABLE IF NOT EXISTS task_runs (
    id               UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    scope            TEXT        NOT NULL,
    task_description TEXT        NOT NULL,
    model_used       TEXT,
    files_changed    TEXT,
    test_results     TEXT,
    outcome          TEXT        NOT NULL CHECK (outcome IN ('success', 'partial', 'failed')),
    notes            TEXT,
    lessons_stored   INTEGER     NOT NULL DEFAULT 0,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_task_runs_scope      ON task_runs(scope);
CREATE INDEX IF NOT EXISTS idx_task_runs_outcome    ON task_runs(outcome);
CREATE INDEX IF NOT EXISTS idx_task_runs_created_at ON task_runs(created_at);

ALTER TABLE memory_signals
    ADD COLUMN IF NOT EXISTS task_run_id UUID REFERENCES task_runs(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_memory_signals_task_run_id ON memory_signals(task_run_id);
