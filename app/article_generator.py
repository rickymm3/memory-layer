"""Generate draft social posts from clusters of public memory atoms.

Format is auto-detected from atom metadata and signal patterns.
Output is written to social_posts as status='draft' for user review.
"""
from __future__ import annotations

import json
import uuid
from typing import Any

import psycopg

from app.config import get_config
from app.llm_provider import get_llm_client

_FORMAT_SYSTEM = (
    "You are an article writer for a knowledge network. "
    "Given a cluster of related beliefs and observations, write a clear, "
    "well-structured article in the requested format. "
    "Write in second-person or neutral voice. Never mention the AI system. "
    "Return ONLY a JSON object with keys: title (string), body (string, markdown OK), "
    "topic_tags (array of strings, max 5). No markdown fences around the JSON."
)

_FORMAT_PROMPTS: dict[str, str] = {
    "tutorial": (
        "Write a step-by-step tutorial or how-to guide based on these beliefs and observations. "
        "Structure it with clear numbered steps. Include practical tips."
    ),
    "discussion": (
        "Write a balanced discussion essay exploring this topic from multiple angles. "
        "Acknowledge disagreements and areas of uncertainty."
    ),
    "open_question": (
        "Write a short post that publicly states the current belief, its confidence level, "
        "and invites the community to provide evidence or corrections. "
        "Be explicit about uncertainty."
    ),
    "news_brief": (
        "Write a concise news brief (2-3 paragraphs) summarizing what multiple independent "
        "sources are saying about this topic. Note any conflicting reports."
    ),
    "narrative": (
        "Write a first-person narrative account of this experience, observation, or project. "
        "Make it personal and specific."
    ),
    "article": (
        "Write a clear, informative article about this topic. "
        "Include relevant context, key points, and practical takeaways."
    ),
}


def _detect_format(atoms: list[dict[str, Any]]) -> str:
    """Infer the best post format from atom types and signal patterns."""
    types = [a.get("memory_type", "") for a in atoms]
    type_counts: dict[str, int] = {}
    for t in types:
        type_counts[t] = type_counts.get(t, 0) + 1

    contested = sum(1 for a in atoms if float(a.get("disagreement_score", 0)) >= 0.4)
    total = len(atoms)

    # High disagreement → discussion or open question
    if contested / max(total, 1) >= 0.4:
        avg_conf = sum(float(a.get("confidence", 0.5)) for a in atoms) / max(total, 1)
        return "open_question" if avg_conf < 0.6 else "discussion"

    # Step-heavy observations → tutorial
    if type_counts.get("observation", 0) >= total * 0.6:
        return "tutorial"

    # Mostly beliefs/decisions → discussion
    belief_types = {"belief", "decision", "opinion", "preference"}
    if sum(type_counts.get(t, 0) for t in belief_types) >= total * 0.5:
        return "discussion"

    # Multiple high-confidence facts from different sources → news brief
    unique_sources = len({a.get("source_user_id") for a in atoms if a.get("source_user_id")})
    if unique_sources >= 2 and type_counts.get("fact", 0) >= 2:
        return "news_brief"

    return "article"


def generate_draft(
    atom_ids: list[str],
    author_username: str,
    format_override: str | None = None,
) -> dict[str, Any] | None:
    """Generate a draft social post from a cluster of atom IDs.

    Returns the new social_posts row dict, or None on failure.
    """
    if not atom_ids:
        return None

    cfg = get_config()

    # Fetch atoms
    atoms: list[dict[str, Any]] = []
    with psycopg.connect(cfg.database_url) as conn:
        with conn.cursor() as cur:
            placeholders = ",".join(["%s"] * len(atom_ids))
            cur.execute(
                f"""
                SELECT id, content, memory_type, scope, confidence, importance,
                       disagreement_score, source_type,
                       ms.source_user_id
                FROM memory_atoms ma
                LEFT JOIN LATERAL (
                    SELECT source_user_id FROM memory_signals
                    WHERE memory_atom_id = ma.id
                    ORDER BY created_at DESC LIMIT 1
                ) ms ON true
                WHERE ma.id IN ({placeholders})
                  AND ma.lifecycle_status = 'active';
                """,
                [str(a) for a in atom_ids],
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
                    "disagreement_score": float(r[6] or 0),
                    "source_type": r[7],
                    "source_user_id": r[8],
                }
                for r in rows
            ]

    if not atoms:
        return None

    fmt = format_override or _detect_format(atoms)
    prompt_extra = _FORMAT_PROMPTS.get(fmt, _FORMAT_PROMPTS["article"])

    atom_summary = "\n".join(
        f"- [{a['memory_type']} conf={a['confidence']:.2f}] {a['content']}"
        for a in atoms
    )
    prompt = (
        f"{prompt_extra}\n\n"
        f"Beliefs and observations to synthesize:\n{atom_summary}\n\n"
        "Return JSON only."
    )

    try:
        llm = get_llm_client()
        raw = llm.generate_response(prompt, system=_FORMAT_SYSTEM, json_mode=True)
        parsed = json.loads(raw) if isinstance(raw, str) else raw
        title = str(parsed.get("title", "Untitled")).strip()
        body = str(parsed.get("body", "")).strip()
        tags = [str(t).strip() for t in parsed.get("topic_tags", []) if t][:5]
    except Exception:
        return None

    if not title or not body:
        return None

    avg_conf = sum(a["confidence"] for a in atoms) / len(atoms)

    with psycopg.connect(cfg.database_url) as conn:
        with conn.cursor() as cur:
            # Resolve author user id
            cur.execute("SELECT id FROM users WHERE username = %s;", (author_username,))
            row = cur.fetchone()
            author_id = str(row[0]) if row else None

            # source_turn_text: first atom's content so /drafts can show
            # "From your conversation: ..." without the user needing to dig in.
            source_hint = atoms[0]["content"][:500] if atoms else None

            cur.execute(
                """
                INSERT INTO social_posts
                    (title, body, format, status, author_user_id,
                     primary_atom_ids, topic_tags, confidence_at_publish,
                     source_turn_text)
                VALUES (%s, %s, %s, 'draft', %s, %s, %s, %s, %s)
                RETURNING id, title, format, status, created_at;
                """,
                (
                    title, body, fmt, author_id,
                    [str(a) for a in atom_ids],
                    tags,
                    round(avg_conf, 3),
                    source_hint,
                ),
            )
            post_row = cur.fetchone()
            post_id = str(post_row[0])

            if author_id:
                cur.execute(
                    """
                    INSERT INTO post_contributors (post_id, user_id, contribution_type, atom_ids)
                    VALUES (%s, %s, 'primary', %s);
                    """,
                    (post_id, author_id, [str(a) for a in atom_ids]),
                )
        conn.commit()

    return {
        "id": post_id,
        "title": title,
        "format": fmt,
        "status": "draft",
        "created_at": str(post_row[4]),
    }


def _has_recent_draft_for_tags(
    author_username: str | None,
    topic_tags: list[str] | None,
    within_days: int = 7,
) -> bool:
    """Return True if the user already has a recent draft or published post covering these tags.

    Prevents N drafts accumulating on the same topic when multiple related atoms
    are committed in quick succession. A 7-day window balances freshness vs spam.
    """
    if not author_username or not topic_tags:
        return False

    cfg = get_config()
    try:
        with psycopg.connect(cfg.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COUNT(*)
                    FROM social_posts sp
                    JOIN users u ON u.id = sp.author_user_id
                    WHERE u.username = %s
                      AND sp.status IN ('draft', 'published')
                      AND sp.topic_tags && %s
                      AND sp.created_at >= now() - interval '1 day' * %s;
                    """,
                    (author_username, topic_tags, within_days),
                )
                row = cur.fetchone()
                return bool(row and int(row[0]) > 0)
    except Exception:
        return False
