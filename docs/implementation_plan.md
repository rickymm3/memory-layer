# Memory-Layer Implementation Plan

This repository now uses a single preflight command as the integration gate before app restructuring:

```bash
.venv/bin/python scripts/check_environment.py
```

The script verifies all Phase 1-3 readiness checks in one run:

1. Ollama reachable
2. Chat model available (`qwen3:8b`)
3. Embedding model available (`qwen3-embedding:latest`)
4. Embedding dimension is 4096
5. Postgres reachable
6. `vector` extension installed
7. `pgcrypto` extension installed
8. `memory_atoms` table exists
9. `embedding` column is `vector(4096)`
10. HNSW status (WARN-only when embedding is `vector(4096)` in this environment)
11. Baseline insert/retrieve works

When `vector(4096)` is used, the doctor prints:

`ANN index not created for vector(4096); exact search is being used for prototype scale.`

This is a warning, not a failure, so milestone work can continue with exact cosine search.

## Configuration

The doctor script loads `.env` using `python-dotenv`.

Expected environment keys:

- `DATABASE_URL`
- `OLLAMA_HOST`
- `CHAT_MODEL`
- `EMBEDDING_MODEL`

If `OLLAMA_HOST` is not set, the script attempts WSL auto-detection using the default route gateway and builds:

`http://<detected-ip>:11434`

## Rule Before Coding

Do not restructure the app layout until `scripts/check_environment.py` returns overall PASS.

## After PASS

After preflight passes, proceed with requested project structure creation and implementation.

Current implemented structure:

- `app/` modules (`config.py`, `ollama_client.py`, `memory_store.py`, `chat.py`)
- `db/init.sql`
 - CLI scripts (`scripts/store_memory.py`, `scripts/retrieve_memory.py`, `scripts/chat_with_memory.py`)
