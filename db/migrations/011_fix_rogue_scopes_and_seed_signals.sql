-- Migration 011: Fix rogue scope atoms and seed missing signals
--
-- Part 1: Normalise atoms whose scopes were not canonical.
--   - Freeform conversational scopes (chat, this interaction, from now on, etc.)
--     were written before the scope normalizer existed.
--   - Secondary project scopes (project:memory-layer-dashboard) are unreachable
--     in chat because scope_filter is always 'project:memory-layer'.
--   - Model scopes (model:qwen3-8b) are project-context observations and should
--     live under the canonical project scope for retrieval to work.
--   - NULL-scope atoms are legacy artefacts; assign to project:memory-layer.

UPDATE memory_atoms
SET scope = 'project:memory-layer'
WHERE scope IN (
    'project:memory-layer-dashboard',
    'model:qwen3-8b',
    'chat',
    'this interaction',
    'from now on',
    'your scope',
    'current system',
    'me',
    'before llms',
    'this research goal',
    'instruction quality',
    'information sources',
    'assistant''s capabilities',
    'the assistant''s thought process',
    'current system'
);

UPDATE memory_atoms
SET scope = 'project:memory-layer'
WHERE scope IS NULL;

-- Part 2: Seed an initial observation signal for any atoms that have none.
--   Atoms stored directly via store_memory() bypass the signal system and have
--   no history for the weight aggregator to work from.

INSERT INTO memory_signals (
    memory_atom_id,
    content,
    memory_type,
    scope,
    relationship,
    confidence,
    importance,
    source_key,
    source_type
)
SELECT
    id,
    content,
    memory_type,
    scope,
    'new',
    confidence,
    importance,
    'system_migration',
    'system'
FROM memory_atoms
WHERE NOT EXISTS (
    SELECT 1 FROM memory_signals ms WHERE ms.memory_atom_id = memory_atoms.id
);
