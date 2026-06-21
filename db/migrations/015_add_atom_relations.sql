-- Sprint 2: Knowledge graph — atom-to-atom relations
-- Allows agents to declare explicit relationships between memory atoms,
-- enabling graph-traversal retrieval (1-hop, 2-hop neighbors).

CREATE TABLE IF NOT EXISTS memory_atom_relations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    atom_a_id UUID NOT NULL REFERENCES memory_atoms(id) ON DELETE CASCADE,
    atom_b_id UUID NOT NULL REFERENCES memory_atoms(id) ON DELETE CASCADE,
    relation_type TEXT NOT NULL DEFAULT 'related'
        CHECK (relation_type IN ('supports','contradicts','specializes','generalizes','related')),
    confidence REAL NOT NULL DEFAULT 0.8 CHECK (confidence BETWEEN 0 AND 1),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    source_key TEXT NOT NULL DEFAULT 'local_user'
);

CREATE INDEX IF NOT EXISTS idx_atom_relations_a ON memory_atom_relations(atom_a_id);
CREATE INDEX IF NOT EXISTS idx_atom_relations_b ON memory_atom_relations(atom_b_id);
