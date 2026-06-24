#!/usr/bin/env python3
"""UserPromptSubmit hook — inject relevant memory atoms before Claude responds.

Fires on every user prompt. Embeds the prompt via Ollama, runs a two-stage
retrieval (broad cosine recall → composite rerank → token-budget gate), and
injects only atoms that clear a quality floor.

Stage 1 — Recall: fetch top-20 candidates by cosine similarity (min 0.20).
Stage 2 — Rerank: composite = (sim*0.35 + conf*0.25 + importance*0.40) × (1 + support_weight*0.5)
Stage 3 — Gate: quality floor 0.25, then fill a hard TOKEN_BUDGET greedily.

The result: only atoms that are simultaneously relevant, credible, and important
reach the context window. Low-confidence or unreinforced atoms are suppressed
even when semantically similar.

Never blocks the prompt on failure — all exceptions exit 0 silently.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path


# ── Tuning constants ──────────────────────────────────────────────────────────
RECALL_LIMIT = 20       # candidates fetched from DB (broad cosine recall)
RECALL_MIN_SIM = 0.20   # loose SQL pre-filter — composite reranker does the real work
QUALITY_FLOOR = 0.25    # composite score minimum; below this → never inject
TOKEN_BUDGET = 400      # hard cap on tokens injected for current atoms
HIST_TOKEN_BUDGET = 120 # hard cap for historical atoms
HIST_LIMIT = 5          # candidates fetched for historical pool


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


def _composite(sim: float, conf: float, importance: float, support_weight: float) -> float:
    """Composite relevance score combining similarity, credibility, and reinforcement."""
    base = sim * 0.35 + conf * 0.25 + importance * 0.40
    return base * (1.0 + float(support_weight or 0) * 0.5)


def _tokens(text: str) -> int:
    return max(1, len(text) // 4)


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
        sys.exit(0)

    if not embedding:
        sys.exit(0)

    vec_str = "[" + ",".join(str(x) for x in embedding) + "]"

    # ── Model-scope from env ───────────────────────────────────────────────────
    model_scope = os.environ.get("MEMORY_MODEL_SCOPE", "model:claude-sonnet-4-6")

    try:
        import psycopg
        with psycopg.connect(database_url, connect_timeout=3) as conn:
            # Stage 1 — broad cosine recall for current atoms
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT content, memory_type, confidence, importance,
                           support_weight,
                           1 - (embedding <=> %s::vector) AS similarity
                    FROM memory_atoms
                    WHERE lifecycle_status IN ('active', 'belief')
                    ORDER BY embedding <=> %s::vector
                    LIMIT %s
                    """,
                    (vec_str, vec_str, RECALL_LIMIT),
                )
                raw_rows = cur.fetchall()

            # Historical pool (evidence/deprecated/superseded)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT content, memory_type, confidence,
                           lifecycle_status,
                           COALESCE(peak_confidence, confidence) AS peak_conf,
                           lifecycle_updated_at,
                           1 - (embedding <=> %s::vector) AS similarity
                    FROM memory_atoms
                    WHERE lifecycle_status IN ('evidence', 'deprecated', 'superseded')
                    ORDER BY embedding <=> %s::vector
                    LIMIT %s
                    """,
                    (vec_str, vec_str, HIST_LIMIT),
                )
                hist_rows = cur.fetchall()

            # Model lessons — always inject active model-scope atoms by importance
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COALESCE(context_summary, content), memory_type, confidence
                    FROM memory_atoms
                    WHERE scope = %s AND lifecycle_status = 'active'
                    ORDER BY importance DESC, confidence DESC
                    LIMIT 5
                    """,
                    (model_scope,),
                )
                model_rows = cur.fetchall()

    except Exception:
        sys.exit(0)

    # Stage 2 — composite reranking of current atoms
    scored: list[tuple[float, str, str]] = []
    for content, mtype, conf, importance, support_weight, sim in raw_rows:
        if sim is None or float(sim) < RECALL_MIN_SIM:
            continue
        score = _composite(
            float(sim),
            float(conf or 0.5),
            float(importance or 0.5),
            float(support_weight or 0),
        )
        scored.append((score, content, mtype))

    scored.sort(reverse=True)

    # Stage 3 — quality floor + token budget gate
    current_lines: list[str] = []
    budget_used = 0
    for score, content, mtype in scored:
        if score < QUALITY_FLOOR:
            break  # sorted DESC; everything below is also below floor
        cost = _tokens(content)
        if budget_used + cost > TOKEN_BUDGET:
            break
        budget_used += cost
        current_lines.append(f"[{mtype}] {content}")

    # Historical atoms — simpler scoring (no composite; just sim + peak_conf)
    hist_lines: list[str] = []
    hist_budget = 0
    for content, mtype, conf, status, peak_conf, updated, sim in hist_rows:
        if sim is None or float(sim) < RECALL_MIN_SIM:
            continue
        cost = _tokens(content)
        if hist_budget + cost > HIST_TOKEN_BUDGET:
            break
        hist_budget += cost
        updated_str = str(updated)[:10] if updated else "unknown"
        hist_lines.append(
            f"[{mtype}|{status} as of {updated_str}, peak conf:{float(peak_conf):.2f}] {content}"
        )

    # Model lessons — compact injection strings
    model_lines: list[str] = []
    model_budget = 0
    MODEL_TOKEN_BUDGET = 300
    for injection_text, mtype, conf in model_rows:
        if not injection_text:
            continue
        cost = _tokens(injection_text)
        if model_budget + cost > MODEL_TOKEN_BUDGET:
            break
        model_budget += cost
        model_lines.append(f"[{mtype}] {injection_text}")

    if not current_lines and not hist_lines and not model_lines:
        sys.exit(0)

    sections: list[str] = ["## MEMORY — RELEVANT CONTEXT"]
    if model_lines:
        sections.append("### Model adaptation directives")
        sections.extend(model_lines)
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
