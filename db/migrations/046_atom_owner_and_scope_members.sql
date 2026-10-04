-- Migration 046: atom ownership + scope membership (team visibility as an access rule).
--
-- 1. memory_atoms.source_user_id — who created the atom. Retrieval already filtered on
--    this column in hosted mode, but it only existed on memory_signals. Backfilled from
--    the atom's earliest signal.
-- 2. scope_members — which users may read visibility='team' atoms in a scope.
--    A 'team' atom in scope project:billing is readable by its owner and by every
--    user listed here for project:billing. Nobody else.

ALTER TABLE memory_atoms
    ADD COLUMN IF NOT EXISTS source_user_id TEXT;

UPDATE memory_atoms a
SET source_user_id = s.source_user_id
FROM (
    SELECT DISTINCT ON (memory_atom_id) memory_atom_id, source_user_id
    FROM memory_signals
    WHERE memory_atom_id IS NOT NULL AND source_user_id IS NOT NULL
    ORDER BY memory_atom_id, created_at ASC
) s
WHERE a.id = s.memory_atom_id
  AND a.source_user_id IS NULL;

CREATE INDEX IF NOT EXISTS idx_memory_atoms_source_user_id ON memory_atoms(source_user_id);
CREATE INDEX IF NOT EXISTS idx_memory_atoms_embedding_model ON memory_atoms(embedding_model);

CREATE TABLE IF NOT EXISTS scope_members (
    scope       TEXT        NOT NULL,
    user_id     TEXT        NOT NULL,
    role        TEXT        NOT NULL DEFAULT 'member' CHECK (role IN ('member', 'owner')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (scope, user_id)
);

CREATE INDEX IF NOT EXISTS idx_scope_members_user_id ON scope_members(user_id);
