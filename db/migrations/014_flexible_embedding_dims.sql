-- Migration 014: Remove hardcoded embedding dimension.
-- Allows any embedding model (OpenAI 1536-dim, qwen3 4096-dim, etc.) to be
-- used without schema changes. pgvector's cosine distance operator (<=>)
-- continues to work on untyped vector columns.
-- If you have an ivfflat or hnsw index on this column, drop and recreate it
-- after running this migration with the correct dimension.
ALTER TABLE memory_atoms ALTER COLUMN embedding TYPE vector USING embedding::vector;
