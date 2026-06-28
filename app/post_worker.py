"""Background post generation worker.

Reads from post_generation_queue, clusters atoms by scope/topic, generates
full posts (title + body) via the article generator, and distributes them
to semantically-similar users as a heartbeat discovery mechanism.

Start with: start_worker()
The worker runs as a daemon thread and processes batches every POLL_INTERVAL seconds.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import Any

import psycopg

from app.config import get_config

_log = logging.getLogger(__name__)
_worker_started = False
_worker_lock = threading.Lock()

POLL_INTERVAL = 30       # seconds between worker sweeps
BATCH_WINDOW = 120       # seconds — collect atoms for this long before generating
MIN_CLUSTER_ATOMS = 1    # minimum atoms per cluster to generate a post
MAX_REACH_USERS = 10     # users to notify on heartbeat distribution


def start_worker() -> None:
    """Start the background post generation worker (idempotent)."""
    global _worker_started
    with _worker_lock:
        if _worker_started:
            return
        _worker_started = True
    t = threading.Thread(target=_worker_loop, daemon=True, name="post-worker")
    t.start()
    _log.info("post_worker: started")


def enqueue(atom_id: str, username: str, scope: str | None) -> None:
    """Add an atom to the generation queue. Called from commit_pipeline."""
    try:
        cfg = get_config()
        with psycopg.connect(cfg.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO post_generation_queue (atom_id, username, scope)
                    VALUES (%s, %s, %s)
                    ON CONFLICT DO NOTHING;
                    """,
                    (atom_id, username or "local_user", scope),
                )
            conn.commit()
    except Exception as exc:
        _log.debug("post_worker.enqueue: failed (non-fatal): %s", exc)


# ── Worker loop ───────────────────────────────────────────────────────────────

def _worker_loop() -> None:
    while True:
        try:
            cfg = get_config()
            _process_pending_perspectives()
            _process_batch()
            _consolidate_drafts_all_users()
            _refresh_regen_tokens(cfg)
            from app.profile_updater import apply_pending_credibility_reactions  # noqa: PLC0415
            apply_pending_credibility_reactions(cfg)
        except Exception as exc:
            _log.warning("post_worker: batch error: %s", exc)
        time.sleep(POLL_INTERVAL)


_REINFORCE_DISTANCE = 0.22   # same idea — reinforce existing atom
_CONTRARY_DISTANCE  = 0.45   # same topic, different angle — check for opposition

_CONTRARY_SYSTEM = (
    "Given two belief statements on the same topic, classify their relationship.\n"
    "contrary: they take opposing positions (one says good, other says bad; one says yes, other says no).\n"
    "supports: they agree or reinforce each other from different angles.\n"
    "unrelated: they share vocabulary but address genuinely different questions.\n"
    "Return ONLY JSON: {\"relation\": \"contrary\" | \"supports\" | \"unrelated\"}. No fences."
)


def _classify_atom_relation(content_a: str, content_b: str) -> str:
    """Return 'contrary', 'supports', or 'unrelated' for two atom contents."""
    import json as _json  # noqa: PLC0415
    from app.llm_provider import get_llm_client  # noqa: PLC0415
    prompt = f"Atom A: {content_a[:300]}\nAtom B: {content_b[:300]}"
    try:
        raw = get_llm_client().generate_response(prompt, system=_CONTRARY_SYSTEM, json_mode=True)
        parsed = _json.loads(raw) if isinstance(raw, str) else raw
        rel = str(parsed.get("relation", "unrelated")).strip().lower()
        return rel if rel in ("contrary", "supports", "unrelated") else "unrelated"
    except Exception:
        return "unrelated"


def _record_atom_relation(atom_id_a: str, atom_id_b: str, relation_type: str, cfg: Any) -> None:
    """Insert into atom_relations (canonical order: smaller UUID first)."""
    a, b = (atom_id_a, atom_id_b) if atom_id_a < atom_id_b else (atom_id_b, atom_id_a)
    try:
        with psycopg.connect(cfg.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO atom_relations (atom_id_a, atom_id_b, relation_type)
                    VALUES (%s::uuid, %s::uuid, %s)
                    ON CONFLICT DO NOTHING;
                    """,
                    (a, b, relation_type),
                )
                if relation_type == "contrary":
                    cur.execute(
                        """
                        UPDATE memory_atoms
                        SET disagreement_score = LEAST(disagreement_score + 0.3, 1.0)
                        WHERE id IN (%s::uuid, %s::uuid);
                        """,
                        (a, b),
                    )
            conn.commit()
    except Exception as exc:
        _log.debug("_record_atom_relation: %s", exc)


def _reinforce_or_link_existing(
    new_atom_id: str,
    content: str,
    username: str,
    cfg: Any,
    store: Any,
) -> str | None:
    """After storing a new atom, scan nearby atoms and:
    - distance < _REINFORCE_DISTANCE: same idea → shouldn't reach here (handled pre-storage)
    - _REINFORCE_DISTANCE ≤ dist < _CONTRARY_DISTANCE: same topic → classify + link as
      contrary or supports, bump disagreement_score if contrary.

    Returns None always (new atom was already created by caller).
    """
    try:
        from app.llm_provider import get_embedding_client  # noqa: PLC0415
        with psycopg.connect(cfg.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT embedding::text FROM memory_atoms WHERE id = %s;", (new_atom_id,))
                row = cur.fetchone()
                if not row or not row[0]:
                    return None
                emb_str = row[0]

                # Find public atoms in the "same topic" zone
                cur.execute(
                    """
                    SELECT id::text, content,
                           (embedding <=> %s::vector) AS dist
                    FROM memory_atoms
                    WHERE visibility = 'public'
                      AND lifecycle_status = 'active'
                      AND id != %s::uuid
                      AND embedding IS NOT NULL
                      AND (embedding <=> %s::vector) BETWEEN %s AND %s
                    ORDER BY dist
                    LIMIT 5;
                    """,
                    (emb_str, new_atom_id, emb_str, _REINFORCE_DISTANCE, _CONTRARY_DISTANCE),
                )
                neighbors = cur.fetchall()

        for existing_id, existing_content, dist in neighbors:
            relation = _classify_atom_relation(content, existing_content)
            if relation in ("contrary", "supports"):
                _record_atom_relation(new_atom_id, existing_id, relation, cfg)
                _log.info(
                    "post_worker: linked %s ↔ %s as %s (dist=%.3f)",
                    new_atom_id[:8], existing_id[:8], relation, dist,
                )
    except Exception as exc:
        _log.debug("_reinforce_or_link_existing: %s", exc)
    return None


def _check_and_reinforce(
    content: str,
    username: str,
    cfg: Any,
    store: Any,
) -> str | None:
    """Embed content, check for near-identical existing public atom.

    If distance < _REINFORCE_DISTANCE: bump confidence, add attribution signal,
    return existing atom_id (caller skips creating a new atom).
    Returns None when content is novel enough to create a new atom.
    """
    try:
        from app.llm_provider import get_embedding_client  # noqa: PLC0415
        embedding = get_embedding_client().embed_text(content)
        emb_str = "[" + ",".join(str(x) for x in embedding) + "]"

        with psycopg.connect(cfg.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id::text, confidence
                    FROM memory_atoms
                    WHERE visibility = 'public'
                      AND lifecycle_status = 'active'
                      AND embedding IS NOT NULL
                      AND (embedding <=> %s::vector) < %s
                    ORDER BY (embedding <=> %s::vector)
                    LIMIT 1;
                    """,
                    (emb_str, _REINFORCE_DISTANCE, emb_str),
                )
                row = cur.fetchone()
                if not row:
                    return None

                existing_id, existing_conf = row
                new_conf = min(float(existing_conf) + 0.08, 1.0)
                cur.execute(
                    "UPDATE memory_atoms SET confidence = %s, updated_at = now() WHERE id = %s;",
                    (new_conf, existing_id),
                )
            conn.commit()

        store.add_signal_to_atom(
            atom_id=existing_id,
            content=content,
            relationship="reinforcement",
            source_key=username or "anonymous",
            source_type="user_perspective",
            source_user_id=username,
        )
        _log.info(
            "post_worker: reinforced atom %s (conf %.2f → %.2f)",
            existing_id[:8], existing_conf, new_conf,
        )
        return existing_id
    except Exception as exc:
        _log.debug("_check_and_reinforce: %s", exc)
        return None


_PERSPECTIVE_EXTRACT_SYSTEM = (
    "You are extracting the core belief or observation from a user's comment on a post. "
    "The user wrote something raw and informal — your job is to find the actual idea underneath it "
    "and express it as a clean, self-contained atomic statement.\n\n"
    "ENTITY-LEVEL FACTS — highest priority rule:\n"
    "When the comment contains specific facts about something the user created, built, or knows "
    "intimately (a game, app, product, personal project, creative work) — do NOT abstract them. "
    "Preserve every name, number, mechanic, rule, measurement, and observation verbatim. "
    "'I built a hex-tile game with 4 resources (wood, stone, gold, influence) for 2-4 players, "
    "45-90 min' must stay as: 'The user built a board game using hex-tile placement with 4 resource "
    "types (wood, stone, gold, influence), designed for 2-4 players with 45-90 minute sessions.' "
    "Abstraction destroys the only knowledge source for novel entities not in any training data. "
    "Set memory_type=fact for entity descriptions. confidence >= 0.85 for first-hand accounts.\n\n"
    "For non-entity comments:\n"
    "- Extract the REAL claim, not the surface phrasing. 'game is bad - why bother' → "
    "'The game lacks the quality needed to justify the effort of engaging with it.'\n"
    "- If the comment is a question, rephrase it as the underlying curiosity or concern.\n"
    "- If it's pure noise with no extractable idea, set content to null.\n"
    "- memory_type: fact | opinion | belief | observation | question | preference | insight\n"
    "- confidence: how certain the commenter seems (0.0–1.0). Casual dismissal = 0.5–0.65. "
    "Considered opinion = 0.7–0.85. Strong conviction = 0.85+.\n"
    "- importance: how central this idea is to understanding the post's topic (0.0–1.0). "
    "Direct evaluation of the post's subject = 0.7+. Tangential comment = 0.3–0.5.\n\n"
    "Return ONLY JSON: {\"content\": string|null, \"memory_type\": string, "
    "\"confidence\": float, \"importance\": float}. No markdown fences."
)


def _extract_perspective_atom(
    raw_text: str,
    post_title: str,
    post_excerpt: str,
) -> dict | None:
    """Run a lightweight LLM extraction on raw perspective text.

    Returns dict with content/memory_type/confidence/importance,
    or None if the LLM rejects the content as extractable.
    """
    import json as _json  # noqa: PLC0415
    from app.llm_provider import get_llm_client  # noqa: PLC0415

    prompt = (
        f"Post: \"{post_title}\"\n"
        f"{post_excerpt[:400]}\n\n"
        f"User comment: \"{raw_text}\"\n\n"
        "Extract the core belief from this comment."
    )
    try:
        llm = get_llm_client()
        raw = llm.generate_response(prompt, system=_PERSPECTIVE_EXTRACT_SYSTEM, json_mode=True)
        parsed = _json.loads(raw) if isinstance(raw, str) else raw
        content = parsed.get("content")
        if not content:
            return None
        return {
            "content": str(content).strip(),
            "memory_type": str(parsed.get("memory_type", "observation")).strip(),
            "confidence": max(0.0, min(1.0, float(parsed.get("confidence", 0.7)))),
            "importance": max(0.0, min(1.0, float(parsed.get("importance", 0.65)))),
        }
    except Exception as exc:
        _log.debug("_extract_perspective_atom: LLM call failed: %s", exc)
        return None


def _process_pending_perspectives() -> None:
    """Extract, embed, and store pending perspectives as public atoms.

    Raw user text goes through a lightweight LLM extraction step first —
    the actual belief is pulled out, classified, and assigned meaningful
    confidence/importance before being stored as a memory atom.

    Bypasses the full commit pipeline (no reconciler, no critic, no risk gate,
    no draft queue) — a perspective is an explicit public contribution that
    skips deliberation but still needs LLM extraction.
    """
    cfg = get_config()
    try:
        with psycopg.connect(cfg.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT p.id::text, p.body, p.author_username, p.post_id::text,
                           sp.title, LEFT(sp.body, 400)
                    FROM perspectives p
                    JOIN social_posts sp ON sp.id = p.post_id
                    WHERE p.atom_id IS NULL
                      AND p.created_at < now() - interval '5 seconds'
                      AND p.created_at > now() - interval '2 hours'
                    ORDER BY p.created_at
                    LIMIT 20;
                    """
                )
                pending = cur.fetchall()
    except Exception as exc:
        _log.debug("post_worker._process_pending_perspectives: fetch failed: %s", exc)
        return

    if not pending:
        return

    from app.memory_store import MemoryStore  # noqa: PLC0415

    store = MemoryStore()

    for persp_id, body, username, post_id, post_title, post_excerpt in pending:
        try:
            # Extract the real belief from raw user text via LLM
            extracted = _extract_perspective_atom(body, post_title or "", post_excerpt or "")
            if not extracted:
                _log.info("post_worker: perspective %s had no extractable content — skipping", persp_id)
                # Mark atom_id with sentinel so it doesn't get retried indefinitely
                with psycopg.connect(cfg.database_url) as _conn:
                    with _conn.cursor() as _cur:
                        _cur.execute(
                            "UPDATE perspectives SET atom_id = 'rejected-by-llm' WHERE id = %s;",
                            (persp_id,),
                        )
                    _conn.commit()
                continue

            # Check for near-identical existing atom (same idea) — reinforce it
            # instead of creating a duplicate. If novel, create a new atom and
            # then scan neighbors to link any contrary or supporting beliefs.
            atom_id = _check_and_reinforce(extracted["content"], username, cfg, store)
            if atom_id is None:
                atom_id, _signal_id = store.store_memory_with_signal(
                    content=extracted["content"],
                    memory_type=extracted["memory_type"],
                    scope="user",
                    confidence=extracted["confidence"],
                    importance=extracted["importance"],
                    relationship="new",
                    source_key=username or "anonymous",
                    source_type="user_perspective",
                    source_user_id=username,
                    visibility="public",
                )
                # Scan nearby atoms and link as contrary/supports where applicable
                _reinforce_or_link_existing(atom_id, extracted["content"], username, cfg, store)

            with psycopg.connect(cfg.database_url) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE perspectives SET atom_id = %s WHERE id = %s;",
                        (atom_id, persp_id),
                    )
                conn.commit()

            # Always regenerate responses on the exact post this perspective
            # was submitted to — we know the post_id, so don't rely on
            # embedding distance to rediscover it.
            from app.response_generator import generate_post_responses  # noqa: PLC0415
            generate_post_responses(post_id)

            # Update contributor's interest vector — writing a perspective is
            # the strongest profile signal we have.
            if username:
                try:
                    from app.profile_updater import record_perspective_contribution  # noqa: PLC0415
                    record_perspective_contribution(username, post_id)
                except Exception:
                    pass

            # If the perspective is substantive (≥100 words), also generate
            # it as a standalone post. It functions as both: a reply to the
            # parent post AND the seed of a new discussion.
            word_count = len(body.split())
            if word_count >= 100 and atom_id and username:
                try:
                    from app.article_generator import generate_draft  # noqa: PLC0415
                    standalone = generate_draft(atom_ids=[atom_id], author_username=username)
                    if standalone:
                        with psycopg.connect(cfg.database_url) as _conn:
                            with _conn.cursor() as _cur:
                                _cur.execute(
                                    "UPDATE perspectives SET standalone_post_id = %s WHERE id = %s;",
                                    (standalone["id"], persp_id),
                                )
                            _conn.commit()
                        _log.info(
                            "post_worker: perspective %s also seeded standalone post %s",
                            persp_id, standalone["id"],
                        )
                except Exception as _exc:
                    _log.debug("post_worker: standalone generation for perspective %s failed: %s", persp_id, _exc)

            # Also propagate to any other similar published posts
            requeue_posts_for_atom(atom_id)
            _log.info(
                "post_worker: perspective %s → atom %s, responses regenerated for post %s",
                persp_id, atom_id, post_id,
            )
        except Exception as exc:
            _log.debug("post_worker: perspective %s store failed: %s", persp_id, exc)


def _process_batch() -> None:
    """Pick up atoms that have been waiting long enough and generate posts."""
    cfg = get_config()
    cutoff = f"now() - interval '{BATCH_WINDOW} seconds'"

    with psycopg.connect(cfg.database_url) as conn:
        with conn.cursor() as cur:
            # Claim a batch of pending items that are old enough to have accumulated context
            cur.execute(
                f"""
                UPDATE post_generation_queue
                SET status = 'processing', attempts = attempts + 1
                WHERE id IN (
                    SELECT id FROM post_generation_queue
                    WHERE status = 'pending' AND enqueued_at < {cutoff}
                    ORDER BY enqueued_at
                    LIMIT 50
                )
                RETURNING id, atom_id::text, username, scope;
                """
            )
            claimed = cur.fetchall()
        conn.commit()

    if not claimed:
        return

    # Group by (username, scope) — generate one post per cluster
    clusters: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for row_id, atom_id, username, scope in claimed:
        key = (username, scope or "general")
        clusters.setdefault(key, []).append((str(row_id), atom_id))

    for (username, scope), items in clusters.items():
        queue_ids = [i[0] for i in items]
        atom_ids = [i[1] for i in items]
        enriched_ids = _enrich_with_related(atom_ids, scope, username, cfg, limit=20)
        if len(enriched_ids) < 3:
            # Not enough material to write a real post — mark done silently
            _mark_queue_items(queue_ids, "done", "insufficient context", cfg)
            _log.debug(
                "post_worker: skipped generation for %s/%s — only %d related atoms",
                username, scope, len(enriched_ids),
            )
            continue
        _generate_and_store(username, scope, enriched_ids, queue_ids, cfg)


def _generate_and_store(
    username: str,
    scope: str,
    atom_ids: list[str],
    queue_ids: list[str],
    cfg: Any,
) -> None:
    """Generate a post from a pre-enriched list of atoms and store it in social_posts.

    Before creating a new draft, checks whether an existing post by this user
    covers the same topic (cosine distance < 0.38). If found: merge atoms into
    that post and regenerate content rather than creating a duplicate.
    """
    try:
        existing = _find_existing_similar_post(atom_ids, username, cfg)
        if existing:
            post_id, post_status = existing
            _merge_atoms_into_post(post_id, post_status, atom_ids, username, cfg)
            _mark_queue_items(queue_ids, "done", None, cfg)
            _log.info(
                "post_worker: merged %d atoms into existing %s post %s for %s",
                len(atom_ids), post_status, post_id, username,
            )
            return

        from app.article_generator import generate_draft  # noqa: PLC0415

        result = generate_draft(atom_ids=atom_ids, author_username=username)
        if not result:
            _mark_queue_items(queue_ids, "failed", "generate_draft returned None", cfg)
            return

        _mark_queue_items(queue_ids, "done", None, cfg)
        post_id = result.get("id")
        if post_id:
            _distribute(post_id, username, atom_ids, cfg)
        _log.info(
            "post_worker: generated post %s for %s (scope=%s, atoms=%d)",
            post_id, username, scope, len(atom_ids),
        )
    except Exception as exc:
        _log.warning("post_worker: generation failed for %s/%s: %s", username, scope, exc)
        _mark_queue_items(queue_ids, "failed", str(exc), cfg)


def _find_existing_similar_post(
    atom_ids: list[str],
    username: str,
    cfg: Any,
) -> tuple[str, str] | None:
    """Return (post_id, status) of an existing draft/published post by this user
    that's semantically close to the incoming atoms (cosine distance < 0.38).

    Returns None if no similar post exists or embeddings are missing.
    """
    try:
        with psycopg.connect(cfg.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT AVG(embedding) AS centroid
                    FROM memory_atoms
                    WHERE id = ANY(%s::uuid[]) AND embedding IS NOT NULL;
                    """,
                    (atom_ids,),
                )
                row = cur.fetchone()
                if not row or row[0] is None:
                    return None
                centroid = row[0]

                cur.execute(
                    """
                    SELECT sp.id::text, sp.status
                    FROM social_posts sp
                    JOIN users u ON u.id = sp.author_user_id
                    WHERE u.username = %s
                      AND sp.status IN ('draft', 'published')
                      AND sp.embedding IS NOT NULL
                      AND (sp.embedding <=> %s::vector) < 0.38
                    ORDER BY (sp.embedding <=> %s::vector)
                    LIMIT 1;
                    """,
                    (username, centroid, centroid),
                )
                row = cur.fetchone()
                return (row[0], row[1]) if row else None
    except Exception as exc:
        _log.debug("post_worker._find_existing_similar_post: %s", exc)
        return None


def _merge_atoms_into_post(
    post_id: str,
    post_status: str,
    atom_ids: list[str],
    username: str,
    cfg: Any,
) -> None:
    """Enrich an existing post with new atoms.

    - For drafts: add atoms to primary_atom_ids and regenerate the body.
    - For published posts: update contributing_atom_ids and refresh consensus
      + AI responses without touching the published body.
    """
    try:
        with psycopg.connect(cfg.database_url) as conn:
            with conn.cursor() as cur:
                if post_status == "draft":
                    cur.execute(
                        """
                        UPDATE social_posts
                        SET primary_atom_ids = ARRAY(
                                SELECT DISTINCT unnest(primary_atom_ids || %s::uuid[])
                            ),
                            updated_at = now()
                        WHERE id = %s::uuid;
                        """,
                        (atom_ids, post_id),
                    )
                else:
                    cur.execute(
                        """
                        UPDATE social_posts
                        SET contributing_atom_ids = ARRAY(
                                SELECT DISTINCT unnest(contributing_atom_ids || %s::uuid[])
                            ),
                            updated_at = now()
                        WHERE id = %s::uuid;
                        """,
                        (atom_ids, post_id),
                    )
            conn.commit()

        from app.consensus_synthesizer import update_post_consensus  # noqa: PLC0415
        from app.response_generator import generate_post_responses  # noqa: PLC0415
        update_post_consensus(post_id)
        generate_post_responses(post_id)

        if post_status == "draft":
            # Regenerate draft body with the now-expanded atom set
            with psycopg.connect(cfg.database_url) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT primary_atom_ids, format
                        FROM social_posts WHERE id = %s::uuid AND status = 'draft';
                        """,
                        (post_id,),
                    )
                    row = cur.fetchone()
            if row:
                all_ids = [str(a) for a in (row[0] or [])]
                fmt = row[1] or "article"
                from app.article_generator import generate_draft  # noqa: PLC0415
                generate_draft(
                    atom_ids=all_ids,
                    author_username=username,
                    format_override=fmt,
                    update_post_id=post_id,
                )
    except Exception as exc:
        _log.debug("post_worker._merge_atoms_into_post: %s", exc)


def _enrich_with_related(
    atom_ids: list[str],
    scope: str,
    username: str,
    cfg: Any,
    limit: int = 20,
) -> list[str]:
    """Expand seed atoms with semantically similar opinion-type atoms from the same user.

    Uses the seed atom's embedding for cosine similarity lookup — not scope matching.
    Only fetches opinion, preference, belief, decision, observation types so post
    generation stays grounded in what the user actually thinks, not world facts.
    """
    _opinion_types = ("opinion", "preference", "belief", "decision", "observation")
    try:
        with psycopg.connect(cfg.database_url) as conn:
            with conn.cursor() as cur:
                # Get the embedding of the first seed atom to anchor similarity search
                cur.execute(
                    """
                    SELECT embedding FROM memory_atoms
                    WHERE id = %s::uuid AND embedding IS NOT NULL
                    LIMIT 1;
                    """,
                    (atom_ids[0],),
                )
                emb_row = cur.fetchone()
                if not emb_row:
                    return atom_ids

                seed_embedding = emb_row[0]

                # Find semantically similar atoms from this user, opinion types only
                placeholders = ",".join(["%s"] * len(_opinion_types))
                cur.execute(
                    f"""
                    SELECT DISTINCT ma.id::text
                    FROM memory_atoms ma
                    JOIN memory_signals ms ON ms.memory_atom_id = ma.id
                    WHERE (ms.source_user_id = %s OR ms.source_user_id IS NULL)
                      AND ma.lifecycle_status = 'active'
                      AND ma.memory_type IN ({placeholders})
                      AND ma.embedding IS NOT NULL
                      AND (ma.embedding <=> %s::vector) < 0.45
                    ORDER BY (ma.embedding <=> %s::vector)
                    LIMIT %s;
                    """,
                    (username, *_opinion_types, seed_embedding, seed_embedding, limit),
                )
                related = [r[0] for r in cur.fetchall()]

        # Merge: seed ids first, then related without duplicates
        seen = set(atom_ids)
        merged = list(atom_ids)
        for aid in related:
            if aid not in seen and len(merged) < limit:
                merged.append(aid)
                seen.add(aid)
        return merged
    except Exception:
        return atom_ids


def _mark_queue_items(
    queue_ids: list[str],
    status: str,
    error: str | None,
    cfg: Any,
) -> None:
    try:
        with psycopg.connect(cfg.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE post_generation_queue
                    SET status = %s, last_error = %s, processed_at = now()
                    WHERE id = ANY(%s::uuid[]);
                    """,
                    (status, error, queue_ids),
                )
            conn.commit()
    except Exception as exc:
        _log.debug("post_worker._mark_queue_items: %s", exc)


# ── Draft consolidation ───────────────────────────────────────────────────────

_CONSOLIDATE_SYSTEM = (
    "You are a writer helping someone publish their ideas. "
    "You have been given the source material — ideas, mechanics, decisions, creative choices — "
    "captured from their conversations about a specific topic. "
    "Write ONE cohesive, engaging post about THAT TOPIC SPECIFICALLY. "
    "Do NOT write about writing, drafting, or content creation. Write about the subject matter itself. "
    "Lead with what's interesting: the concept, the creative spark, what makes it worth reading.\n\n"
    "FORMATTING — use markdown structure:\n"
    "- Strong opening paragraph (no heading)\n"
    "- ## headings for 2–4 major sections\n"
    "- **bold** for key terms or emphasis\n"
    "- Bullet lists where they genuinely help (not everywhere)\n"
    "- Punchy closing paragraph\n"
    "Aim for 400–700 words. Real piece, not a summary.\n\n"
    "Return ONLY a JSON object: {\"title\": string, \"body\": string, "
    "\"topic_tags\": array max 5}. No markdown fences around the JSON."
)


def _consolidate_drafts_all_users() -> None:
    """Run draft consolidation for all users who have fragmented drafts."""
    try:
        cfg = get_config()
        with psycopg.connect(cfg.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT DISTINCT u.username
                    FROM social_posts sp
                    JOIN users u ON u.id = sp.author_user_id
                    WHERE sp.status = 'draft'
                    GROUP BY u.username
                    HAVING COUNT(*) >= 2;
                    """
                )
                usernames = [r[0] for r in cur.fetchall()]
        for username in usernames:
            try:
                _consolidate_user_drafts(username, cfg)
            except Exception as exc:
                _log.debug("post_worker.consolidate: failed for %s: %s", username, exc)
    except Exception as exc:
        _log.debug("post_worker._consolidate_drafts_all_users: %s", exc)


def _consolidate_user_drafts(username: str, cfg: Any) -> None:
    """Find clusters of overlapping drafts for a user and merge each cluster."""
    with psycopg.connect(cfg.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT sp.id::text, sp.title, sp.topic_tags, sp.primary_atom_ids
                FROM social_posts sp
                JOIN users u ON u.id = sp.author_user_id
                WHERE u.username = %s AND sp.status = 'draft'
                  AND sp.created_at < now() - interval '10 minutes'
                ORDER BY sp.created_at;
                """,
                (username,),
            )
            rows = cur.fetchall()

    if len(rows) < 2:
        return

    import re as _re

    def _norm_tag(t: str) -> str:
        """Normalise a tag for comparison: lowercase, collapse separators."""
        return _re.sub(r"[\s_\-]+", " ", t.strip().lower())

    def _title_keywords(title: str) -> set[str]:
        """Extract significant words from a title for cross-post matching."""
        stop = {"the", "a", "an", "of", "in", "and", "or", "for", "to",
                "is", "are", "its", "with", "on", "at", "by"}
        return {w for w in _re.sub(r"[^a-z0-9 ]", "", title.lower()).split()
                if len(w) > 3 and w not in stop}

    # Build normalised-tag AND keyword → post_id lookup maps
    from collections import defaultdict as _dd
    norm_tag_to_posts: dict[str, list[str]] = _dd(list)
    keyword_to_posts: dict[str, list[str]] = _dd(list)
    post_data: dict[str, dict] = {}
    for post_id, title, tags, atom_ids in rows:
        norm_tags = {_norm_tag(t) for t in (tags or [])}
        kws = _title_keywords(title)
        post_data[post_id] = {
            "title": title,
            "norm_tags": norm_tags,
            "keywords": kws,
            "atom_ids": [str(a) for a in (atom_ids or [])],
        }
        for nt in norm_tags:
            norm_tag_to_posts[nt].append(post_id)
        for kw in kws:
            keyword_to_posts[kw].append(post_id)

    # Find clusters: posts that share ≥ 2 normalised tags OR ≥ 3 title keywords
    visited: set[str] = set()
    clusters: list[list[str]] = []
    for post_id in post_data:
        if post_id in visited:
            continue
        cluster = {post_id}
        # Gather candidates from both tag and keyword overlap
        candidates: set[str] = set()
        for nt in post_data[post_id]["norm_tags"]:
            candidates.update(norm_tag_to_posts.get(nt, []))
        for kw in post_data[post_id]["keywords"]:
            candidates.update(keyword_to_posts.get(kw, []))
        for neighbour in candidates:
            if neighbour == post_id or neighbour in visited:
                continue
            shared_tags = post_data[post_id]["norm_tags"] & post_data[neighbour]["norm_tags"]
            shared_words = post_data[post_id]["keywords"] & post_data[neighbour]["keywords"]
            if len(shared_tags) >= 2 or len(shared_words) >= 3:
                cluster.add(neighbour)
        if len(cluster) >= 2:
            clusters.append(list(cluster))
            visited.update(cluster)

    for cluster_ids in clusters:
        _merge_cluster(username, cluster_ids, post_data, cfg)


def _merge_cluster(
    username: str,
    cluster_ids: list[str],
    post_data: dict[str, dict],
    cfg: Any,
) -> None:
    """Synthesize a cluster of fragmented drafts into one cohesive post."""
    import json as _json  # noqa: PLC0415

    # Collect all atom IDs across the cluster (deduplicated)
    seen: set[str] = set()
    all_atom_ids: list[str] = []
    all_titles: list[str] = []
    for pid in cluster_ids:
        all_titles.append(post_data[pid]["title"])
        for aid in post_data[pid]["atom_ids"]:
            if aid not in seen:
                all_atom_ids.append(aid)
                seen.add(aid)

    if not all_atom_ids:
        return

    try:
        llm = _get_chat_client()
        fragment_summary = "\n".join(f"- {t}" for t in all_titles)
        # Fetch atom content for richer synthesis
        with psycopg.connect(cfg.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT content FROM memory_atoms
                    WHERE id = ANY(%s::uuid[]) AND lifecycle_status = 'active'
                    LIMIT 30;
                    """,
                    (all_atom_ids,),
                )
                atom_texts = [r[0] for r in cur.fetchall()]

        atoms_block = "\n".join(f"- {t}" for t in atom_texts)
        prompt = (
            f"These fragmented drafts all cover the same topic:\n{fragment_summary}\n\n"
            f"Core ideas and facts from the source conversations:\n{atoms_block}\n\n"
            "Write ONE cohesive, engaging post that brings this all together. "
            "Lead with the most interesting hook. Tell the full story."
        )
        raw = llm.generate_response(prompt, system=_CONSOLIDATE_SYSTEM, json_mode=True)
        parsed = _json.loads(raw) if isinstance(raw, str) else raw
        title = (parsed.get("title") or "").strip()
        body = (parsed.get("body") or "").strip()
        tags = [str(t).strip() for t in parsed.get("topic_tags", []) if t][:5]

        if not title or not body:
            return

        with psycopg.connect(cfg.database_url) as conn:
            from app.utils import unique_slug  # noqa: PLC0415
            slug = unique_slug(conn, title)

            with conn.cursor() as cur:
                # Resolve author user ID
                cur.execute("SELECT id FROM users WHERE username = %s", (username,))
                row = cur.fetchone()
                if not row:
                    return
                author_id = row[0]

                # Insert the consolidated post
                cur.execute(
                    """
                    INSERT INTO social_posts
                        (title, body, format, status, author_user_id, primary_atom_ids, topic_tags, slug)
                    VALUES (%s, %s, 'article', 'draft', %s, %s::uuid[], %s, %s)
                    RETURNING id;
                    """,
                    (title, body, author_id, all_atom_ids, tags, slug),
                )
                new_id = cur.fetchone()[0]

                # Archive the fragmented originals
                cur.execute(
                    "UPDATE social_posts SET status = 'archived' WHERE id = ANY(%s::uuid[])",
                    (cluster_ids,),
                )
            conn.commit()

        _log.info(
            "post_worker: consolidated %d drafts → post %s for %s",
            len(cluster_ids), new_id, username,
        )
    except Exception as exc:
        _log.debug("post_worker._merge_cluster: %s", exc)


def _get_chat_client():
    from app.llm_provider import get_llm_client  # noqa: PLC0415
    return get_llm_client()


# ── Regen token refresh ───────────────────────────────────────────────────────

def _refresh_regen_tokens(cfg: Any) -> None:
    """Reset regen_tokens to 5 for any user whose 24-hour window has elapsed."""
    try:
        with psycopg.connect(cfg.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE users
                    SET regen_tokens = 5,
                        regen_tokens_reset_at = now()
                    WHERE regen_tokens_reset_at < now() - interval '24 hours'
                      AND regen_tokens < 5;
                    """
                )
                updated = cur.rowcount
            conn.commit()
        if updated:
            _log.info("post_worker: refreshed regen tokens for %d users", updated)
    except Exception as exc:
        _log.debug("post_worker._refresh_regen_tokens: %s", exc)


# ── Heartbeat distribution ────────────────────────────────────────────────────

def _distribute(post_id: str, author_username: str, atom_ids: list[str], cfg: Any) -> None:
    """Push a new post to a small set of users whose beliefs are semantically similar.

    This is the 'heartbeat feeler': a post starts with limited reach (up to
    MAX_REACH_USERS) and expands only if those users engage with it. Promoted
    to /popular when reach_score crosses the promotion threshold.
    """
    try:
        candidates = _find_similar_users(atom_ids, author_username, cfg)
        if not candidates:
            return

        post_uuid = uuid.UUID(post_id)
        with psycopg.connect(cfg.database_url) as conn:
            with conn.cursor() as cur:
                for user_id in candidates[:MAX_REACH_USERS]:
                    cur.execute(
                        """
                        INSERT INTO post_reach (post_id, user_id)
                        VALUES (%s, %s)
                        ON CONFLICT DO NOTHING;
                        """,
                        (post_uuid, uuid.UUID(user_id)),
                    )
            conn.commit()
        _log.debug("post_worker: distributed post %s to %d users", post_id, len(candidates))
    except Exception as exc:
        _log.debug("post_worker._distribute: %s", exc)


def _find_similar_users(atom_ids: list[str], exclude_username: str, cfg: Any) -> list[str]:
    """Find user IDs whose private atom embeddings are semantically close to this post's atoms.

    Uses cosine similarity across all atom embeddings for the given IDs to find
    users who would find this content relevant, without exposing their private data.
    """
    try:
        with psycopg.connect(cfg.database_url) as conn:
            with conn.cursor() as cur:
                # Average the embeddings of the post's atoms as a query vector
                cur.execute(
                    """
                    SELECT AVG(embedding) as centroid
                    FROM memory_atoms
                    WHERE id = ANY(%s::uuid[]) AND embedding IS NOT NULL;
                    """,
                    (atom_ids,),
                )
                row = cur.fetchone()
                if not row or row[0] is None:
                    return []
                centroid = row[0]

                # Find users whose atoms are semantically close to the centroid
                # Exclude the post author; return user IDs ordered by similarity
                cur.execute(
                    """
                    SELECT DISTINCT u.id::text
                    FROM memory_atoms ma
                    JOIN memory_signals ms ON ms.memory_atom_id = ma.id
                    JOIN users u ON u.username = ms.source_user_id
                    WHERE ma.embedding IS NOT NULL
                      AND ms.source_user_id != %s
                      AND ma.lifecycle_status = 'active'
                      AND (ma.embedding <=> %s::vector) < 0.35
                    ORDER BY u.id
                    LIMIT %s;
                    """,
                    (exclude_username, centroid, MAX_REACH_USERS),
                )
                return [r[0] for r in cur.fetchall()]
    except Exception:
        return []


# ── Living post updater ───────────────────────────────────────────────────────

def requeue_posts_for_atom(atom_id: str) -> None:
    """Call when a new or updated atom may affect existing posts.

    Two triggers:
    1. Exact match — posts that directly cite this atom in primary_atom_ids
    2. Embedding similarity — published posts whose topic embedding is within
       0.38 cosine distance of this atom (cross-user consensus update)
    """
    try:
        cfg = get_config()
        with psycopg.connect(cfg.database_url) as conn:
            with conn.cursor() as cur:
                # 1. Exact-match posts (original author's drafts)
                cur.execute(
                    """
                    SELECT sp.id::text, u.username, ma.scope
                    FROM social_posts sp
                    JOIN users u ON u.id = sp.author_user_id
                    JOIN memory_atoms ma ON ma.id = %s::uuid
                    WHERE sp.primary_atom_ids @> ARRAY[%s::uuid]
                      AND sp.status = 'draft';
                    """,
                    (atom_id, atom_id),
                )
                exact_rows = cur.fetchall()

                # 2. Embedding-similar published posts (consensus update)
                cur.execute(
                    """
                    SELECT sp.id::text
                    FROM social_posts sp
                    JOIN memory_atoms ma ON ma.id = %s::uuid
                    WHERE sp.embedding IS NOT NULL
                      AND ma.embedding IS NOT NULL
                      AND sp.status = 'published'
                      AND (sp.embedding <=> ma.embedding) < 0.38;
                    """,
                    (atom_id,),
                )
                similar_post_ids = [r[0] for r in cur.fetchall()]

            # Re-enqueue exact-match drafts for full regeneration
            with conn.cursor() as cur:
                for post_id, username, scope in exact_rows:
                    cur.execute(
                        """
                        INSERT INTO post_generation_queue (atom_id, username, scope)
                        VALUES (%s, %s, %s);
                        """,
                        (atom_id, username, scope),
                    )
            conn.commit()

        if exact_rows:
            _log.info("post_worker: requeued %d drafts for atom %s", len(exact_rows), atom_id)

        # Update consensus and responses on similar published posts without full regen
        if similar_post_ids:
            from app.consensus_synthesizer import update_post_consensus  # noqa: PLC0415
            from app.response_generator import generate_post_responses  # noqa: PLC0415
            for post_id in similar_post_ids:
                update_post_consensus(post_id)
                generate_post_responses(post_id)
            _log.info(
                "post_worker: refreshed consensus+responses on %d posts for atom %s",
                len(similar_post_ids), atom_id,
            )

    except Exception as exc:
        _log.debug("post_worker.requeue_posts_for_atom: %s", exc)
