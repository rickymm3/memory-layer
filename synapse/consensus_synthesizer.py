"""Cross-user consensus synthesis for Synapse posts.

A post's original body is immutable. This module produces the living
consensus_body — a synthesis of all users' atoms whose embeddings are
semantically similar to the post's topic, weighted by trust and confidence.

Output reads as consensus journalism: majority view, contested territory,
fringe positions — each grounded in the atom evidence.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import psycopg

from app.config import get_config
from app.llm_provider import get_llm_client

_log = logging.getLogger(__name__)

SIMILARITY_THRESHOLD = 0.38   # cosine distance — atoms closer than this feed the consensus
MAX_ATOMS_PER_USER = 10
MAX_TOTAL_ATOMS = 60

_CONSENSUS_SYSTEM = (
    "You are synthesizing a consensus response for a community knowledge post. "
    "You have been given atoms (beliefs, observations, decisions) from multiple contributors, "
    "each with a confidence score, a disagreement score, and a trust weight for their author.\n\n"
    "Your job is to write ONE cohesive synthesis that honestly represents the distribution of views:\n"
    "- Lead with the majority position if one exists (high confidence, low disagreement)\n"
    "- Name contested territory directly: 'Views differ on X — some hold that..., while others argue...'\n"
    "- Surface fringe or emerging positions as such: 'A smaller group believes..., citing...'\n"
    "- Ground every claim in the actual atom evidence — never invent positions\n"
    "- Use because/because/because chains: explain WHY people hold each view, not just WHAT they hold\n"
    "- imp (importance) signals how central this is to the contributor: high imp → give it prominent placement; low imp → supporting detail only\n\n"
    "Tone rules:\n"
    "- Match the emotional register of the source material — do not flatten grief, excitement, or anger\n"
    "- Never use promotional language or call-to-action phrases\n"
    "- Write in third-person editorial voice, not first-person\n"
    "- 150–400 words. Dense and grounded, not padded.\n\n"
    "Return ONLY a JSON object: {\"consensus\": string}. No markdown fences."
)


def _classify_atoms(atoms: list[dict]) -> dict[str, list[dict]]:
    """Split atoms into majority, contested, and fringe buckets."""
    majority, contested, fringe = [], [], []
    for a in atoms:
        conf = float(a.get("confidence", 0.7))
        dis = float(a.get("disagreement_score", 0.0))
        weight = float(a.get("trust_weight", 1.0))
        if dis >= 0.4:
            contested.append(a)
        elif conf < 0.55 or weight < 0.3:
            fringe.append(a)
        else:
            majority.append(a)
    return {"majority": majority, "contested": contested, "fringe": fringe}


def synthesize_consensus(post_id: str, post_embedding: Any) -> str | None:
    """Generate a consensus body for a post from cross-user atom evidence.

    Returns the consensus text string, or None if there's insufficient data
    or the LLM call fails. Does NOT write to the database — caller handles that.
    """
    cfg = get_config()

    try:
        with psycopg.connect(cfg.database_url) as conn:
            with conn.cursor() as cur:
                # Find atoms from all users semantically close to this post
                cur.execute(
                    """
                    SELECT
                        ma.id::text,
                        ma.content,
                        ma.memory_type,
                        ma.confidence,
                        ma.importance,
                        COALESCE(ma.disagreement_score, 0.0) AS disagreement_score,
                        ms.source_user_id,
                        COALESCE(u.usefulness_score, 1.0) AS trust_weight,
                        (ma.embedding <=> %s::vector) AS distance
                    FROM memory_atoms ma
                    JOIN memory_signals ms ON ms.memory_atom_id = ma.id
                    LEFT JOIN users u ON u.username = ms.source_user_id
                    WHERE ma.embedding IS NOT NULL
                      AND ma.lifecycle_status = 'active'
                      AND ma.visibility = 'public'
                      AND (ma.embedding <=> %s::vector) < %s
                    ORDER BY distance ASC
                    LIMIT %s;
                    """,
                    (post_embedding, post_embedding, SIMILARITY_THRESHOLD, MAX_TOTAL_ATOMS),
                )
                rows = cur.fetchall()
    except Exception as exc:
        _log.warning("consensus_synthesizer: atom fetch failed for post %s: %s", post_id, exc)
        return None

    if not rows:
        return None

    atoms = [
        {
            "id": r[0],
            "content": r[1],
            "memory_type": r[2],
            "confidence": float(r[3]),
            "importance": float(r[4]),
            "disagreement_score": float(r[5]),
            "source_user": r[6] or "unknown",
            "trust_weight": float(r[7]),
            "distance": float(r[8]),
        }
        for r in rows
    ]

    buckets = _classify_atoms(atoms)

    def _fmt_bucket(label: str, items: list[dict]) -> str:
        if not items:
            return ""
        lines = "\n".join(
            f"  [{a['memory_type']} conf={a['confidence']:.2f} imp={a['importance']:.2f} "
            f"dis={a['disagreement_score']:.2f} trust={a['trust_weight']:.2f}] {a['content'][:300]}"
            for a in items
        )
        return f"{label}:\n{lines}"

    sections = [
        _fmt_bucket("MAJORITY / HIGH CONFIDENCE", buckets["majority"]),
        _fmt_bucket("CONTESTED / DISAGREED", buckets["contested"]),
        _fmt_bucket("FRINGE / LOW CONFIDENCE", buckets["fringe"]),
    ]
    atom_block = "\n\n".join(s for s in sections if s)

    if not atom_block.strip():
        return None

    prompt = (
        f"Post ID: {post_id}\n\n"
        f"Contributor atoms (grouped by epistemic status):\n{atom_block}\n\n"
        "Synthesize the consensus response."
    )

    try:
        llm = get_llm_client()
        raw = llm.generate_response(prompt, system=_CONSENSUS_SYSTEM, json_mode=True)
        parsed = json.loads(raw) if isinstance(raw, str) else raw
        consensus = str(parsed.get("consensus", "")).strip()
        return consensus or None
    except Exception as exc:
        _log.warning("consensus_synthesizer: LLM call failed for post %s: %s", post_id, exc)
        return None


def update_post_consensus(post_id: str) -> bool:
    """Fetch the post embedding and regenerate its consensus_body in-place.

    Returns True if consensus was written, False otherwise.
    """
    cfg = get_config()

    try:
        with psycopg.connect(cfg.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT embedding, contributing_atom_ids FROM social_posts WHERE id = %s;",
                    (post_id,),
                )
                row = cur.fetchone()
                if not row or row[0] is None:
                    return False
                post_embedding, _ = row

        consensus = synthesize_consensus(post_id, post_embedding)
        if not consensus:
            return False

        with psycopg.connect(cfg.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE social_posts
                    SET consensus_body = %s, consensus_updated_at = now()
                    WHERE id = %s;
                    """,
                    (consensus, post_id),
                )
            conn.commit()

        _log.info("consensus_synthesizer: updated consensus for post %s", post_id)
        return True

    except Exception as exc:
        _log.warning("consensus_synthesizer: update_post_consensus failed for %s: %s", post_id, exc)
        return False
