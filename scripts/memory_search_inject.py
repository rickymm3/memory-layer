#!/usr/bin/env python3
"""UserPromptSubmit hook — inject relevant memory atoms before Claude responds.

Fires on every user prompt. Embeds the prompt via Ollama, searches Postgres
for semantically similar atoms, and injects them as additionalContext so the
LLM has memory context before generating its response.

Must be fast — runs synchronously before Claude sees the user message.
Uses direct HTTP + psycopg with short timeouts; never blocks the prompt.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path


def _load_env(project_dir: str) -> None:
    env_path = Path(project_dir) / ".env"
    if not env_path.exists():
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def _embed(prompt: str, ollama_host: str, model: str) -> list[float]:
    data = json.dumps({"model": model, "input": prompt}).encode()
    req = urllib.request.Request(
        f"{ollama_host}/api/embed",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=4) as resp:
        body = json.loads(resp.read())
    return body.get("embeddings", [[]])[0]


def main() -> None:
    try:
        raw = sys.stdin.read()
        data = json.loads(raw)
    except Exception:
        sys.exit(0)

    prompt = (data.get("prompt") or "").strip()
    if not prompt:
        sys.exit(0)

    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", "")
    _load_env(project_dir)

    database_url = os.environ.get("DATABASE_URL", "")
    ollama_host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
    embed_model = os.environ.get("EMBEDDING_MODEL", "nomic-embed-text")

    if not database_url:
        sys.exit(0)

    try:
        embedding = _embed(prompt[:600], ollama_host, embed_model)
    except Exception:
        sys.exit(0)  # Ollama unavailable — don't block prompt

    if not embedding:
        sys.exit(0)

    vec_str = "[" + ",".join(str(x) for x in embedding) + "]"

    try:
        import psycopg
        with psycopg.connect(database_url, connect_timeout=3) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT content, memory_type, confidence, scope,
                           1 - (embedding <=> %s::vector) AS similarity,
                           lifecycle_status,
                           peak_confidence
                    FROM memory_atoms
                    WHERE lifecycle_status IN ('active', 'belief')
                    ORDER BY embedding <=> %s::vector
                    LIMIT 6
                    """,
                    (vec_str, vec_str),
                )
                rows = cur.fetchall()

            # Also grab top historical atoms for the same query
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT content, memory_type, confidence, scope,
                           1 - (embedding <=> %s::vector) AS similarity,
                           lifecycle_status,
                           COALESCE(peak_confidence, confidence) AS peak_conf,
                           lifecycle_updated_at
                    FROM memory_atoms
                    WHERE lifecycle_status IN ('evidence', 'deprecated', 'superseded')
                    ORDER BY embedding <=> %s::vector
                    LIMIT 3
                    """,
                    (vec_str, vec_str),
                )
                hist_rows = cur.fetchall()

    except Exception:
        sys.exit(0)

    MIN_SIM = 0.35
    current_lines: list[str] = []
    for content, mtype, conf, scope, sim, status, peak_conf in rows:
        if sim and float(sim) >= MIN_SIM:
            current_lines.append(f"[{mtype}] (conf:{float(conf):.2f}) {content}")

    hist_lines: list[str] = []
    for content, mtype, conf, scope, sim, status, peak_conf, updated in hist_rows:
        if sim and float(sim) >= MIN_SIM:
            updated_str = str(updated)[:10] if updated else "unknown date"
            hist_lines.append(
                f"[{mtype}|{status} as of {updated_str}, peak conf:{float(peak_conf):.2f}] {content}"
            )

    if not current_lines and not hist_lines:
        sys.exit(0)

    sections: list[str] = ["## MEMORY — RELEVANT CONTEXT"]
    if current_lines:
        sections.append("### Current beliefs")
        sections.extend(current_lines)
    if hist_lines:
        sections.append("### Historical (no longer active — preserved for context)")
        sections.extend(hist_lines)

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": "\n".join(sections),
        }
    }))


if __name__ == "__main__":
    main()
