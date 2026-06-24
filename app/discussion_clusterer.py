"""Groups public memory atoms into living discussion threads.

Two triggers for assignment:
  - confidence * importance >= CLUSTER_THRESHOLD (community-validated)
  - interest_flag = true (LLM-flagged novelty, bypasses threshold)

Run via: make cluster-discussions
"""
from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

import psycopg

from app.config import get_config
from app.db import get_store
from app.llm_provider import get_llm_client as get_llm

log = logging.getLogger(__name__)

CLUSTER_THRESHOLD = 0.45        # confidence * importance floor
SIMILARITY_THRESHOLD = 0.72     # cosine similarity to join existing discussion
NOVELTY_THRESHOLD = 0.75        # novelty_score to set interest_flag on discussion

_TITLE_SYSTEM = (
    "You generate concise, engaging discussion titles for groups of related ideas. "
    "Return ONLY valid JSON with a single key 'title'. No markdown fences."
)


def _generate_title(atoms: list[dict[str, Any]]) -> str:
    llm = get_llm()
    excerpts = "\n".join(f"- {a['content'][:120]}" for a in atoms[:5])
    prompt = (
        f"These ideas form a discussion thread:\n{excerpts}\n\n"
        "Generate a short, specific discussion title (max 10 words). "
        'Return: {"title": "..."}'
    )
    try:
        raw = llm.generate_response(prompt, system=_TITLE_SYSTEM, json_mode=True)
        parsed = json.loads(raw[raw.find("{"):raw.rfind("}") + 1])
        return parsed.get("title", "Untitled Discussion")
    except Exception:
        return atoms[0]["content"][:60] + "…" if atoms else "Untitled Discussion"


def _qualifying_unassigned_atoms(conn) -> list[dict[str, Any]]:
    """Return public active atoms not yet in any discussion that qualify for clustering."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ma.id, ma.content, ma.memory_type, ma.scope,
                   ma.confidence, ma.importance, ma.novelty_score, ma.interest_flag,
                   ma.embedding,
                   ms.source_user_id
            FROM memory_atoms ma
            JOIN memory_signals ms ON ms.memory_atom_id = ma.id
            LEFT JOIN discussion_atoms da ON da.atom_id = ma.id
            WHERE ma.visibility = 'public'
              AND ma.lifecycle_status = 'active'
              AND ma.embedding IS NOT NULL
              AND da.atom_id IS NULL
              AND (
                  ma.interest_flag = true
                  OR (ma.confidence * COALESCE(ma.importance, 0.5)) >= %s
              )
            GROUP BY ma.id, ms.source_user_id
            ORDER BY ma.novelty_score DESC, (ma.confidence * COALESCE(ma.importance, 0.5)) DESC
            LIMIT 200;
            """,
            (CLUSTER_THRESHOLD,),
        )
        rows = cur.fetchall()
    return [
        {
            "id": str(r[0]),
            "content": r[1],
            "memory_type": r[2],
            "topic_tags": [t for t in (r[3] or "").split(":") if t][:3],
            "confidence": float(r[4]),
            "importance": float(r[5] or 0.5),
            "novelty_score": float(r[6]),
            "interest_flag": bool(r[7]),
            "embedding": r[8],
            "source_user_id": r[9],
        }
        for r in rows
    ]


def _find_matching_discussion(conn, store, embedding, exclude_ids: set[str]) -> str | None:
    """Return the best matching discussion id by comparing to seed atom embeddings."""
    if not exclude_ids:
        # Compare against all existing discussions
        with conn.cursor() as cur:
            cur.execute("SELECT id, seed_atom_ids FROM discussions LIMIT 500;")
            discussions = cur.fetchall()
    else:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, seed_atom_ids FROM discussions WHERE id != ALL(%s) LIMIT 500;",
                (list(exclude_ids),),
            )
            discussions = cur.fetchall()

    emb_str = embedding if isinstance(embedding, str) else store._vector_literal(embedding)
    best_id = None
    best_sim = 0.0

    for disc_id, seed_ids in discussions:
        if not seed_ids:
            continue
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT 1 - (embedding <=> '{emb_str}'::vector) AS sim
                FROM memory_atoms
                WHERE id = ANY(%s) AND embedding IS NOT NULL
                ORDER BY sim DESC
                LIMIT 1;
                """,
                (seed_ids,),
            )
            row = cur.fetchone()
        if row and float(row[0]) > best_sim:
            best_sim = float(row[0])
            best_id = str(disc_id)

    return best_id if best_sim >= SIMILARITY_THRESHOLD else None


def _notify_existing_participants(conn, discussion_id: str, new_user: str) -> None:
    """Send a notification to all users already in this discussion (except the new one)."""
    with conn.cursor() as cur:
        # Get distinct users already in this discussion
        cur.execute(
            """
            SELECT DISTINCT da.source_user_id, u.id AS user_uuid
            FROM discussion_atoms da
            JOIN users u ON u.username = da.source_user_id
            WHERE da.discussion_id = %s AND da.source_user_id != %s;
            """,
            (discussion_id, new_user),
        )
        participants = cur.fetchall()

    for _, user_uuid in participants:
        with conn.cursor() as cur:
            # Upsert: increment count if notification already exists
            cur.execute(
                """
                INSERT INTO user_notifications(user_id, discussion_id, new_atom_count)
                VALUES (%s, %s, 1)
                ON CONFLICT DO NOTHING;
                """,
                (str(user_uuid), discussion_id),
            )


def run_once() -> dict[str, int]:
    cfg = get_config()
    store = get_store()
    stats = {"atoms_processed": 0, "discussions_created": 0, "discussions_updated": 0}

    with psycopg.connect(cfg.database_url) as conn:
        atoms = _qualifying_unassigned_atoms(conn)
        log.info("discussion_clusterer: %d qualifying atoms", len(atoms))

        for atom in atoms:
            stats["atoms_processed"] += 1
            disc_id = _find_matching_discussion(conn, store, atom["embedding"], set())

            if disc_id:
                # Add to existing discussion
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO discussion_atoms(discussion_id, atom_id, source_user_id, novelty_score)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT(discussion_id, atom_id) DO NOTHING;
                        """,
                        (disc_id, atom["id"], atom["source_user_id"], atom["novelty_score"]),
                    )
                    cur.execute(
                        """
                        UPDATE discussions SET
                            atom_count = atom_count + 1,
                            contributor_count = (
                                SELECT COUNT(DISTINCT source_user_id)
                                FROM discussion_atoms WHERE discussion_id = %s
                            ),
                            novelty_flag = novelty_flag OR %s,
                            last_activity_at = now()
                        WHERE id = %s;
                        """,
                        (disc_id, atom["novelty_score"] >= NOVELTY_THRESHOLD, disc_id),
                    )
                _notify_existing_participants(conn, disc_id, atom["source_user_id"])
                conn.commit()
                stats["discussions_updated"] += 1
            else:
                # Create new discussion seeded by this atom
                title = _generate_title([atom])
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO discussions(title, topic_tags, seed_atom_ids,
                            contributor_count, atom_count, novelty_flag)
                        VALUES (%s, %s, %s, 1, 1, %s)
                        RETURNING id;
                        """,
                        (
                            title,
                            atom["topic_tags"],
                            [atom["id"]],
                            atom["novelty_score"] >= NOVELTY_THRESHOLD,
                        ),
                    )
                    new_disc_id = str(cur.fetchone()[0])
                    cur.execute(
                        """
                        INSERT INTO discussion_atoms(discussion_id, atom_id, source_user_id, novelty_score)
                        VALUES (%s, %s, %s, %s);
                        """,
                        (new_disc_id, atom["id"], atom["source_user_id"], atom["novelty_score"]),
                    )
                conn.commit()
                stats["discussions_created"] += 1

    return stats


def main() -> None:
    import argparse
    import time

    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--interval", type=int, default=900, help="seconds between runs")
    args = parser.parse_args()

    while True:
        result = run_once()
        print(f"clusterer: {result}")
        if not args.loop:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
