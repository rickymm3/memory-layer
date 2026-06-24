-- Thread status system: tracks where each discussion is in the knowledge loop.
-- Statuses mirror the Synapse loop lifecycle exactly.
ALTER TABLE discussions
    ADD COLUMN IF NOT EXISTS thread_status TEXT NOT NULL DEFAULT 'active'
        CHECK (thread_status IN (
            'active',      -- AI is processing / newly created
            'gathering',   -- Routed to forum, awaiting human responses
            'updated',     -- Atom written, originating user not yet notified
            'answered',    -- Confident atom committed, answer delivered
            'validated',   -- Post-hoc usefulness confirmed by user
            'reopened',    -- New evidence arrived, loop re-engaged
            'unresolved'   -- No strong answer found yet
        )),
    -- Link back to the question that originated this thread (nullable: clustered discussions have none)
    ADD COLUMN IF NOT EXISTS question_atom_id UUID REFERENCES memory_atoms(id) ON DELETE SET NULL,
    -- Who asked (nullable: system-generated discussions have no single owner)
    ADD COLUMN IF NOT EXISTS created_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_discussions_thread_status ON discussions(thread_status);
CREATE INDEX IF NOT EXISTS idx_discussions_created_by ON discussions(created_by_user_id)
    WHERE created_by_user_id IS NOT NULL;
