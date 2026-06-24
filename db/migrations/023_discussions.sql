-- Novelty fields on memory_atoms (LLM-assigned during critic pass)
ALTER TABLE memory_atoms
    ADD COLUMN IF NOT EXISTS novelty_score FLOAT NOT NULL DEFAULT 0.0
        CHECK (novelty_score >= 0 AND novelty_score <= 1),
    ADD COLUMN IF NOT EXISTS interest_flag BOOLEAN NOT NULL DEFAULT false;

CREATE INDEX IF NOT EXISTS idx_memory_atoms_interest ON memory_atoms(interest_flag) WHERE interest_flag = true;

-- Living discussion threads — auto-clustered from atom embeddings
CREATE TABLE IF NOT EXISTS discussions (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title               TEXT NOT NULL,
    topic_tags          TEXT[]    NOT NULL DEFAULT '{}',
    seed_atom_ids       UUID[]    NOT NULL DEFAULT '{}',
    contributor_count   INT       NOT NULL DEFAULT 0,
    atom_count          INT       NOT NULL DEFAULT 0,
    novelty_flag        BOOLEAN   NOT NULL DEFAULT false,
    last_activity_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Atoms that belong to each discussion (deduped per atom)
CREATE TABLE IF NOT EXISTS discussion_atoms (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    discussion_id   UUID NOT NULL REFERENCES discussions(id) ON DELETE CASCADE,
    atom_id         UUID NOT NULL REFERENCES memory_atoms(id) ON DELETE CASCADE,
    source_user_id  TEXT NOT NULL,
    novelty_score   FLOAT NOT NULL DEFAULT 0.0,
    added_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(discussion_id, atom_id)
);

CREATE INDEX IF NOT EXISTS idx_discussion_atoms_discussion ON discussion_atoms(discussion_id);
CREATE INDEX IF NOT EXISTS idx_discussion_atoms_user ON discussion_atoms(source_user_id);

-- Per-user unread alerts
CREATE TABLE IF NOT EXISTS user_notifications (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    discussion_id   UUID NOT NULL REFERENCES discussions(id) ON DELETE CASCADE,
    new_atom_count  INT  NOT NULL DEFAULT 1,
    read            BOOLEAN NOT NULL DEFAULT false,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_user_notifications_user_unread
    ON user_notifications(user_id, read) WHERE read = false;
