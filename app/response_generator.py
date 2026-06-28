"""AI-generated multi-response synthesis for Synapse posts.

For each published post, this module finds semantically related memory atoms,
clusters them by belief similarity, and generates one LLM-written response per
cluster. The result is a ranked list of distinct community viewpoints — not a
single consensus, but multiple voices from the collective knowledge base.

Responses are stored in post_ai_responses and ordered by reach_score (engagement).
"""
from __future__ import annotations

import json
import logging
from typing import Any

import numpy as np
import psycopg

from app.config import get_config
from app.llm_provider import get_llm_client

_log = logging.getLogger(__name__)

SIMILARITY_THRESHOLD = 0.40   # cosine distance — atoms within this feed responses
CLUSTER_SIMILARITY   = 0.55   # cosine similarity threshold for merging into a cluster
MAX_CLUSTERS         = 7      # cap to avoid UI clutter
MAX_ATOMS_PER_QUERY  = 60
MAX_ATOMS_PER_CLUSTER = 8

_SYNTHESIS_SYSTEM = (
    "You are writing the primary community reply to a post — the synthesis of what the "
    "knowledge corpus believes about this topic.\n\n"
    "Write 2-4 sentences that:\n"
    "- Engage directly with what the post is asking or arguing\n"
    "- Bring in the atom insights as backing evidence or a sharpening question\n"
    "- Feel like a knowledgeable community member replying, not writing an article\n\n"
    "Use atom metadata to calibrate voice:\n"
    "- conf >0.8 → state it with conviction. conf <0.5 → frame as a question or uncertainty.\n"
    "- imp >0.7 → this is the contributor's core point — reflect its weight.\n"
    "- imp <0.4 → background detail — brief mention only.\n\n"
    "ATTRIBUTION — credit contributors in natural language when warranted:\n"
    "- imp >0.7 and a 'by:@username' is present → weave credit into the text naturally: "
    "'as @alice pointed out', '@bob noted that', 'building on @carol\\'s observation...'\n"
    "- imp ≤0.4 or no username → no need to attribute; synthesize anonymously.\n"
    "- Never list usernames as a bullet or footnote — attribution must read as prose.\n\n"
    "Rules:\n"
    "- This is a REPLY — never write a headline or standalone article intro\n"
    "- Never use 'This view holds...', 'A thread of thinking...' — those are essay framings\n"
    "- Address the post's specific claim — do not drift to the general topic\n"
    "- Ground every claim in the atoms — do not invent positions\n"
    "- Match the emotional register of the post\n"
    "- 60-120 words. Specific and direct.\n\n"
    "Return ONLY a JSON object: {\"response\": string}. No markdown fences."
)

_FOLLOWUP_SYSTEM = (
    "You are writing a follow-up reply to a post. An initial synthesis already exists. "
    "A new cluster of beliefs has emerged — your job is to decide whether this cluster "
    "DISPUTES the existing synthesis or ADDS a new angle it missed, then write accordingly.\n\n"
    "Dispute: the atoms challenge, contradict, or complicate the existing reply. "
    "Write as a direct pushback — 'Actually...', 'That misses...', 'The harder question is...'\n\n"
    "Addition: the atoms bring something genuinely new the synthesis didn't cover. "
    "Write as an extension — 'Worth adding...', 'One thing this leaves out...', "
    "or simply lead with the new point.\n\n"
    "ATTRIBUTION — same as synthesis: when an atom has imp >0.7 and a 'by:@username', "
    "credit the contributor naturally in the prose. Do not list names as a footer.\n\n"
    "Rules:\n"
    "- Make clear from the first sentence whether you are disputing or adding\n"
    "- This is a REPLY — not a standalone article or headline\n"
    "- Ground every claim in the atoms — do not invent positions\n"
    "- Match the emotional register of the post\n"
    "- 60-120 words. Specific and direct.\n\n"
    "Return ONLY a JSON object: "
    "{\"response\": string, \"kind\": \"dispute\" | \"addition\"}. No markdown fences."
)


def _cosine_sim(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two float vectors."""
    va, vb = np.array(a, dtype=np.float32), np.array(b, dtype=np.float32)
    denom = np.linalg.norm(va) * np.linalg.norm(vb)
    if denom == 0:
        return 0.0
    return float(np.dot(va, vb) / denom)


def _centroid(embeddings: list[list[float]]) -> list[float]:
    return np.mean(np.array(embeddings, dtype=np.float32), axis=0).tolist()


def _cluster_atoms(atoms: list[dict]) -> list[list[dict]]:
    """Greedy semantic clustering. Each atom joins the nearest cluster if similarity
    exceeds CLUSTER_SIMILARITY, otherwise starts a new one. Cap at MAX_CLUSTERS."""
    clusters: list[list[dict]] = []
    centroids: list[list[float]] = []

    for atom in atoms:
        emb = atom.get("embedding")
        if emb is None:
            continue

        best_idx, best_sim = -1, -1.0
        for i, centroid in enumerate(centroids):
            sim = _cosine_sim(emb, centroid)
            if sim > best_sim:
                best_sim = sim
                best_idx = i

        if best_sim >= CLUSTER_SIMILARITY and best_idx >= 0:
            clusters[best_idx].append(atom)
            cluster_embs = [a["embedding"] for a in clusters[best_idx] if a.get("embedding")]
            centroids[best_idx] = _centroid(cluster_embs)
        elif len(clusters) < MAX_CLUSTERS:
            clusters.append([atom])
            centroids.append(list(emb))

    return clusters


def _generate_response_for_cluster(
    cluster: list[dict],
    post_title: str,
    post_excerpt: str,
    anchor_synthesis: str | None = None,
) -> tuple[str, str, list[str]] | None:
    """Call the LLM to write one reply for this atom cluster.

    Returns (body, kind, contributing_usernames) where kind is
    'synthesis' | 'dispute' | 'addition'.
    Returns None if the LLM call fails or produces empty output.

    When anchor_synthesis is None, writes the primary synthesis reply.
    When provided, the LLM decides whether this cluster disputes or adds to it.
    """
    atoms_block_lines = []
    for a in cluster[:MAX_ATOMS_PER_CLUSTER]:
        meta = f"[{a['memory_type']} conf={a['confidence']:.2f} imp={a.get('importance', 0):.2f}"
        if a.get("contributor") and a.get("importance", 0) > 0.4:
            meta += f" by:@{a['contributor']}"
        meta += "]"
        atoms_block_lines.append(f"- {meta} {a['content'][:300]}")
    atoms_block = "\n".join(atoms_block_lines)

    # Collect credited contributors — those with imp > 0.7 (primary drivers)
    credited = list(dict.fromkeys(
        a["contributor"]
        for a in cluster[:MAX_ATOMS_PER_CLUSTER]
        if a.get("contributor") and a.get("importance", 0) > 0.7
    ))

    if anchor_synthesis is None:
        system = _SYNTHESIS_SYSTEM
        prompt = (
            f"POST:\nTitle: {post_title}\n{post_excerpt}\n\n"
            f"---\nRelated beliefs from the community:\n{atoms_block}\n\n"
            "Write the primary reply to this post."
        )
        default_kind = "synthesis"
    else:
        system = _FOLLOWUP_SYSTEM
        prompt = (
            f"POST:\nTitle: {post_title}\n{post_excerpt}\n\n"
            f"---\nExisting synthesis reply:\n{anchor_synthesis}\n\n"
            f"---\nNew cluster of beliefs:\n{atoms_block}\n\n"
            "Does this cluster dispute or add to the existing reply? Write accordingly."
        )
        default_kind = "addition"

    try:
        llm = get_llm_client()
        raw = llm.generate_response(prompt, system=system, json_mode=True)
        parsed = json.loads(raw) if isinstance(raw, str) else raw
        text = str(parsed.get("response", "")).strip()
        kind = parsed.get("kind", default_kind)
        if kind not in ("synthesis", "dispute", "addition"):
            kind = default_kind
        return (text, kind, credited) if text else None
    except Exception as exc:
        _log.debug("response_generator: LLM call failed: %s", exc)
        return None


def generate_post_responses(post_id: str) -> bool:
    """Find semantically related atoms, cluster them, write one response per cluster.

    Deletes any existing responses for the post and replaces them with the new
    set. Returns True if at least one response was stored.
    """
    cfg = get_config()

    # 1. Fetch post embedding + title + body excerpt (context for reply generation)
    try:
        with psycopg.connect(cfg.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT embedding, title, body FROM social_posts WHERE id = %s;",
                    (post_id,),
                )
                row = cur.fetchone()
                if not row or row[0] is None:
                    return False
                post_embedding = row[0]
                post_title    = row[1] or ""
                # First 800 chars of body is enough for the LLM to understand the topic
                post_excerpt  = (row[2] or "")[:800]
    except Exception as exc:
        _log.warning("response_generator: post fetch failed for %s: %s", post_id, exc)
        return False

    # 2. Find related atoms from all users (public only)
    try:
        with psycopg.connect(cfg.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        ma.id::text,
                        ma.content,
                        ma.memory_type,
                        ma.confidence,
                        ma.importance,
                        ma.embedding::text,
                        (ma.embedding <=> %s::vector) AS distance,
                        (
                            SELECT ms.source_user_id
                            FROM memory_signals ms
                            WHERE ms.memory_atom_id = ma.id
                              AND ms.source_user_id IS NOT NULL
                            ORDER BY ms.created_at ASC
                            LIMIT 1
                        ) AS contributor_username
                    FROM memory_atoms ma
                    WHERE ma.embedding IS NOT NULL
                      AND ma.lifecycle_status = 'active'
                      AND ma.visibility = 'public'
                      AND COALESCE(ma.source_type, '') != 'ai_generated'
                      AND (ma.embedding <=> %s::vector) < %s
                    ORDER BY distance ASC
                    LIMIT %s;
                    """,
                    (post_embedding, post_embedding, SIMILARITY_THRESHOLD, MAX_ATOMS_PER_QUERY),
                )
                rows = cur.fetchall()
    except Exception as exc:
        _log.warning("response_generator: atom fetch failed for %s: %s", post_id, exc)
        return False

    if not rows:
        _log.debug("response_generator: no public atoms within threshold for post %s", post_id)
        return False

    # Parse embedding strings returned by psycopg as "[0.1,0.2,...]"
    atoms = []
    for r in rows:
        emb_raw = r[5]
        try:
            if isinstance(emb_raw, str):
                emb = json.loads(emb_raw)
            else:
                emb = list(emb_raw)
        except Exception:
            emb = None
        atoms.append({
            "id": r[0],
            "content": r[1],
            "memory_type": r[2],
            "confidence": float(r[3]),
            "importance": float(r[4]),
            "embedding": emb,
            "distance": float(r[6]),
            "contributor": r[7] or None,
        })

    # 3. Cluster atoms by semantic similarity
    clusters = _cluster_atoms(atoms)
    if not clusters:
        return False

    # 4. Generate responses: first cluster = anchor synthesis, rest = dispute or addition
    responses: list[tuple[str, str, list[str], list[str]]] = []  # (body, kind, atom_ids, usernames)
    anchor_synthesis: str | None = None

    for i, cluster in enumerate(clusters):
        result = _generate_response_for_cluster(
            cluster,
            post_title,
            post_excerpt,
            anchor_synthesis=anchor_synthesis if i > 0 else None,
        )
        if result:
            text, kind, credited_users = result
            atom_ids = [a["id"] for a in cluster]
            responses.append((text, kind, atom_ids, credited_users))
            if i == 0:
                anchor_synthesis = text  # first response anchors all subsequent ones

    if not responses:
        return False

    # 5. Replace existing responses for this post
    try:
        with psycopg.connect(cfg.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM post_ai_responses WHERE post_id = %s;",
                    (post_id,),
                )
                for body, kind, atom_ids, credited_users in responses:
                    cur.execute(
                        """
                        INSERT INTO post_ai_responses
                            (post_id, body, source_atom_ids, response_kind, contributing_usernames)
                        VALUES (%s, %s, %s::uuid[], %s, %s);
                        """,
                        (post_id, body, atom_ids, kind, credited_users),
                    )
            conn.commit()

        _log.info(
            "response_generator: stored %d responses for post %s",
            len(responses), post_id,
        )
    except Exception as exc:
        _log.warning("response_generator: store failed for %s: %s", post_id, exc)
        return False

    # 6. Commit AI responses back into memory_atoms so user corrections can
    #    reinforce or refine them. Excluded from future generation queries via
    #    source_type='ai_generated' filter — prevents echo-loop compounding.
    try:
        from app.db import get_store as _get_store  # noqa: PLC0415
        store = _get_store()
        for body, kind, _atom_ids, _users in responses:
            if not body or len(body) < 30:
                continue
            context = f"[AI {kind} on '{post_title}']: {body}"
            try:
                store.store_memory_with_signal(
                    content=context,
                    context_summary=f"AI-generated {kind} reply for post: {post_title}",
                    memory_type="insight",
                    scope="user",
                    confidence=0.65,
                    importance=0.5,
                    relationship="new",
                    source_key="synapse_ai",
                    source_type="ai_generated",
                    visibility="public",
                )
            except Exception as exc:
                _log.debug("response_generator: atom commit failed for response: %s", exc)
    except Exception as exc:
        _log.debug("response_generator: atom commit phase failed (non-fatal): %s", exc)

    return True
