from __future__ import annotations

import requests

from app.config import get_config
from app.db import get_store


def get_memory_health() -> dict:
    """Return memory-layer health status.

    Never exposes DATABASE_URL, OLLAMA_HOST, file paths, or stack traces.
    """
    result: dict = {
        "status": "ok",
        "atom_count": None,
        "available_scopes": None,
        "embedding_model": None,
        "db_reachable": False,
        "ollama_reachable": False,
        "message": None,
    }

    try:
        config = get_config()
    except Exception:
        result["status"] = "error"
        result["message"] = "configuration error"
        return result

    # Check Ollama reachability (only relevant when using Ollama embeddings)
    if config.ollama_host:
        try:
            resp = requests.get(f"{config.ollama_host}/api/version", timeout=5)
            resp.raise_for_status()
            result["ollama_reachable"] = True
        except Exception:
            result["ollama_reachable"] = False

    # Check database reachability and count atoms
    try:
        stats = get_store().health_stats()
        result["atom_count"] = stats["atom_count"]
        result["available_scopes"] = stats["available_scopes"]
        result["db_reachable"] = True
        result["embedding_model"] = config.embedding_model
        result["backend"] = stats.get("backend", "unknown")
    except Exception:
        result["db_reachable"] = False

    problems = []
    if not result["db_reachable"]:
        problems.append("database unreachable")
    if config.ollama_host and not result["ollama_reachable"]:
        problems.append("ollama unreachable")
    if problems:
        result["status"] = "error"
        result["message"] = "; ".join(problems)

    return result
