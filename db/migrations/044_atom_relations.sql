-- Links between atoms that express opposing or supporting positions on the same topic.
-- relation_type:
--   'contrary'  — the two atoms take opposing positions (increases disagreement_score on both)
--   'supports'  — the two atoms reinforce the same position
-- Rows are stored with atom_id_a < atom_id_b (canonical order) to prevent duplicates.

CREATE TABLE IF NOT EXISTS atom_relations (
    atom_id_a   UUID NOT NULL REFERENCES memory_atoms(id) ON DELETE CASCADE,
    atom_id_b   UUID NOT NULL REFERENCES memory_atoms(id) ON DELETE CASCADE,
    relation_type TEXT NOT NULL CHECK (relation_type IN ('contrary', 'supports')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (atom_id_a, atom_id_b)
);

CREATE INDEX IF NOT EXISTS idx_atom_relations_b ON atom_relations(atom_id_b);
