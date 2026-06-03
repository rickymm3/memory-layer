from __future__ import annotations

import requests
import psycopg

from app.config import get_config


def get_memory_health() -> dict:
    """Return memory-layer health status.

    Never exposes DATABASE_URL, OLLAMA_HOST, file paths, or stack traces.
    On failure, returns a generic error message only.
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

    # Check Ollama reachability
    try:
        resp = requests.get(f"{config.ollama_host}/api/version", timeout=5)
        resp.raise_for_status()
        result["ollama_reachable"] = True
    except Exception:
        result["ollama_reachable"] = False

    # Check database reachability and count atoms
    try:
        with psycopg.connect(config.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM memory_atoms;")
                row = cur.fetchone()
                result["atom_count"] = int(row[0]) if row else 0
                cur.execute(
                    "SELECT DISTINCT scope FROM memory_atoms ORDER BY scope;"
                )
                result["available_scopes"] = [r[0] for r in cur.fetchall()]
        result["db_reachable"] = True
        result["embedding_model"] = config.embedding_model
    except Exception:
        result["db_reachable"] = False

    problems = []
    if not result["db_reachable"]:
        problems.append("database unreachable")
    if not result["ollama_reachable"]:
        problems.append("ollama unreachable")
    if problems:
        result["status"] = "error"
        result["message"] = "; ".join(problems)

    return result
