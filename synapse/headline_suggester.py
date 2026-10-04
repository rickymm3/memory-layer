"""Proactive headline suggestions from a user's memory atoms.

Scans the user's atoms, groups them by scope/topic, and generates one
headline per cluster via individual LLM calls. This approach is more
reliable than asking a local LLM for an array in a single call.
"""
from __future__ import annotations

import json
from typing import Any

import psycopg

from app.config import get_config
from app.llm_provider import get_llm_client

_VALID_FORMATS = {"article", "tutorial", "discussion", "open_question", "narrative", "news_brief"}

# Cache TTL — regenerate even if no new atoms arrive after this many seconds.
_CACHE_TTL = 3600  # 1 hour

_CACHE_INIT = False  # tracks whether we've ensured the table exists this process


def _ensure_cache_table(conn: psycopg.Connection) -> None:
    global _CACHE_INIT
    if _CACHE_INIT:
        return
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS headline_suggestion_cache (
                username        TEXT PRIMARY KEY,
                suggestions     JSONB NOT NULL DEFAULT '[]',
                generated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                last_atom_ts    TEXT
            );
        """)
    conn.commit()
    _CACHE_INIT = True


def _latest_atom_ts(username: str, cur: psycopg.Cursor) -> str | None:
    """Return the MAX created_at of signals for this user as a string, or None."""
    try:
        cur.execute(
            """
            SELECT MAX(created_at)::text FROM memory_signals
            WHERE source_user_id = %s OR source_user_id IS NULL;
            """,
            (username,),
        )
        row = cur.fetchone()
        return row[0] if row else None
    except Exception:
        return None


def _read_cache(username: str, conn: psycopg.Connection) -> list | None:
    """Return cached suggestions if still fresh, else None."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT suggestions, generated_at, last_atom_ts
                FROM headline_suggestion_cache
                WHERE username = %s;
                """,
                (username,),
            )
            row = cur.fetchone()
            if not row:
                return None
            suggestions, generated_at, cached_atom_ts = row
            import datetime as _dt
            age = (_dt.datetime.now(_dt.timezone.utc) - generated_at).total_seconds()
            if age > _CACHE_TTL:
                return None
            current_atom_ts = _latest_atom_ts(username, cur)
            if current_atom_ts != cached_atom_ts:
                return None
            return suggestions if isinstance(suggestions, list) else []
    except Exception:
        return None


def _write_cache(username: str, suggestions: list, conn: psycopg.Connection) -> None:
    try:
        with conn.cursor() as cur:
            atom_ts = _latest_atom_ts(username, cur)
            cur.execute(
                """
                INSERT INTO headline_suggestion_cache (username, suggestions, generated_at, last_atom_ts)
                VALUES (%s, %s::jsonb, NOW(), %s)
                ON CONFLICT (username) DO UPDATE SET
                    suggestions  = EXCLUDED.suggestions,
                    generated_at = EXCLUDED.generated_at,
                    last_atom_ts = EXCLUDED.last_atom_ts;
                """,
                (username, json.dumps(suggestions), atom_ts),
            )
        conn.commit()
    except Exception:
        pass


def invalidate_cache(username: str) -> None:
    """Delete cached suggestions so the next page load regenerates them."""
    try:
        cfg = get_config()
        with psycopg.connect(cfg.database_url) as conn:
            _ensure_cache_table(conn)
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM headline_suggestion_cache WHERE username = %s;",
                    (username,),
                )
            conn.commit()
    except Exception:
        pass

_SINGLE_HEADLINE_SYSTEM = (
    "You are an editorial assistant for a personal knowledge publishing platform. "
    "Given a cluster of a user's memory atoms (beliefs, observations, decisions), "
    "suggest ONE publishable article headline that best represents this cluster. "
    'Return ONLY a JSON object: {"headline":"...", "rationale":"...", '
    '"format":"article", "atom_ids":["uuid1","uuid2"]} '
    "format must be one of: article, tutorial, discussion, open_question, narrative, news_brief. "
    "No markdown fences, no arrays, no extra text."
)


def _cluster_atoms(atoms: list[dict], max_clusters: int = 5) -> list[tuple[str, list[dict]]]:
    """Group atoms into clusters by scope, returning (label, atoms) pairs.

    Single-atom scopes are bundled into an 'other' group once there are enough.
    """
    scope_groups: dict[str, list[dict]] = {}
    for atom in atoms:
        scope = atom.get("scope") or "general"
        # Normalize scope: strip project: prefix so "project:memory-layer" → "memory-layer"
        if ":" in scope:
            label = scope.split(":", 1)[1]
        else:
            label = scope
        scope_groups.setdefault(label, []).append(atom)

    # Sort groups by size descending; split into large (2+) and small (1)
    large = [(k, v) for k, v in scope_groups.items() if len(v) >= 2]
    small = [(k, v) for k, v in scope_groups.items() if len(v) < 2]

    clusters = sorted(large, key=lambda x: len(x[1]), reverse=True)[:max_clusters]

    # Bundle small groups into "general" if there are enough atoms
    leftover = [a for _, group in small for a in group]
    if leftover and len(clusters) < max_clusters:
        clusters.append(("general insights", leftover))

    return clusters


def _call_llm_for_headline(
    llm: Any,
    cluster_label: str,
    cluster_atoms: list[dict],
) -> dict[str, Any] | None:
    """Ask the LLM for a single headline from a cluster of atoms."""
    atom_lines = "\n".join(
        f"[id={a['id']} type={a['memory_type']} conf={a['confidence']:.2f}] {a['content'][:200]}"
        for a in cluster_atoms
    )
    prompt = (
        f"Topic cluster: {cluster_label}\n\n"
        f"Memory atoms:\n{atom_lines}\n\n"
        "Return one headline as a JSON object (no array)."
    )
    try:
        raw = llm.generate_response(prompt, system=_SINGLE_HEADLINE_SYSTEM, json_mode=True)
        parsed = json.loads(raw) if isinstance(raw, str) else raw
        # Unwrap accidental arrays
        if isinstance(parsed, list) and parsed:
            parsed = parsed[0]
        if not isinstance(parsed, dict):
            return None
        headline = str(parsed.get("headline", "")).strip()
        if not headline:
            return None
        fmt = str(parsed.get("format", "article")).strip().lower().replace(" ", "_")
        if fmt not in _VALID_FORMATS:
            fmt = "article"
        known_ids = {a["id"] for a in cluster_atoms}
        atom_ids = [str(aid) for aid in parsed.get("atom_ids", []) if aid and str(aid) in known_ids]
        # Fall back to top cluster atom IDs if LLM returned nothing valid
        if not atom_ids:
            atom_ids = [a["id"] for a in cluster_atoms[:10]]
        return {
            "headline": headline,
            "rationale": str(parsed.get("rationale", "")).strip(),
            "format": fmt,
            "atom_ids": atom_ids,
            "topic_group": cluster_label,
        }
    except Exception:
        return None


def suggest_headlines(
    username: str,
    max_atoms: int = 40,
    min_atoms: int = 2,
    max_suggestions: int = 5,
) -> list[dict[str, Any]]:
    """Return headline suggestion dicts for the given user.

    Each dict has: headline, rationale, format, atom_ids, topic_group.
    Results are cached in PostgreSQL so all gunicorn workers share the same cache.
    Returns [] if the user has fewer than min_atoms atoms or all LLM calls fail.
    """
    cfg = get_config()
    atoms: list[dict] = []

    try:
        with psycopg.connect(cfg.database_url) as conn:
            _ensure_cache_table(conn)

            # Return cached suggestions if still fresh and atoms haven't changed
            cached = _read_cache(username, conn)
            if cached is not None:
                return cached
            with conn.cursor() as cur:
                # Include atoms owned by this user AND atoms written without a
                # user context (e.g. Claude CLI writing via MCP stdio mode).
                cur.execute(
                    """
                    SELECT DISTINCT ON (ma.id)
                           ma.id, ma.content, ma.memory_type, ma.scope,
                           ma.confidence, ma.importance
                    FROM memory_atoms ma
                    JOIN memory_signals ms ON ms.memory_atom_id = ma.id
                    WHERE (ms.source_user_id = %s OR ms.source_user_id IS NULL)
                      AND ma.lifecycle_status = 'active'
                      AND ma.visibility IN ('public', 'private')
                    ORDER BY ma.id, ma.importance DESC, ma.confidence DESC
                    LIMIT %s;
                    """,
                    (username, max_atoms),
                )
                rows = cur.fetchall()
                atoms = [
                    {
                        "id": str(r[0]),
                        "content": r[1],
                        "memory_type": r[2],
                        "scope": r[3],
                        "confidence": float(r[4]),
                        "importance": float(r[5]),
                    }
                    for r in rows
                ]
    except Exception:
        return []

    if len(atoms) < min_atoms:
        return []

    clusters = _cluster_atoms(atoms, max_clusters=max_suggestions)
    if not clusters:
        return []

    try:
        llm = get_llm_client()
    except Exception:
        return []

    results = []
    for label, cluster in clusters:
        suggestion = _call_llm_for_headline(llm, label, cluster)
        if suggestion:
            results.append(suggestion)
        if len(results) >= max_suggestions:
            break

    # Persist to PostgreSQL so all gunicorn workers share the same cached results
    try:
        with psycopg.connect(cfg.database_url) as conn:
            _write_cache(username, results, conn)
    except Exception:
        pass

    return results
