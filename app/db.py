from __future__ import annotations

from app.config import get_config


def get_store():
    """Return the active backend store (Postgres or SQLite) based on config."""
    cfg = get_config()
    if cfg.backend == "postgres":
        from app.memory_store import MemoryStore
        return MemoryStore()
    from app.sqlite_store import SQLiteStore
    return SQLiteStore(cfg.sqlite_db_path)
