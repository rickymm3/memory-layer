"""Publish trigger: monitor public atoms and generate draft posts when thresholds are met.

Run as a one-shot check or in a loop:
    python -m app.publish_trigger
    python -m app.publish_trigger --loop
"""
from __future__ import annotations

import argparse
import time
import uuid
from typing import Any

import psycopg

from app.article_generator import generate_draft
from app.config import get_config

PUBLISH_THRESHOLD = 0.65   # confidence × importance
MIN_UNIQUE_SOURCES = 1     # minimum distinct signal sources before drafting


def find_qualifying_clusters() -> list[dict[str, Any]]:
    """Return public atoms that pass the publish threshold and have no draft yet."""
    cfg = get_config()
    with psycopg.connect(cfg.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT ma.id, ma.content, ma.memory_type, ma.scope,
                       ma.confidence, ma.importance,
                       ma.confidence * ma.importance AS score,
                       ma.unique_source_count,
                       ms_latest.source_user_id
                FROM memory_atoms ma
                LEFT JOIN LATERAL (
                    SELECT source_user_id FROM memory_signals
                    WHERE memory_atom_id = ma.id
                    ORDER BY created_at DESC LIMIT 1
                ) ms_latest ON true
                WHERE ma.visibility = 'public'
                  AND ma.lifecycle_status = 'active'
                  AND ma.confidence * ma.importance >= %s
                  AND COALESCE(ma.unique_source_count, 0) >= %s
                  AND ma.id NOT IN (
                    SELECT unnest(primary_atom_ids) FROM social_posts
                    WHERE status IN ('draft', 'published')
                  )
                ORDER BY score DESC
                LIMIT 20;
                """,
                (PUBLISH_THRESHOLD, MIN_UNIQUE_SOURCES),
            )
            rows = cur.fetchall()

    return [
        {
            "id": str(r[0]),
            "content": r[1],
            "memory_type": r[2],
            "scope": r[3],
            "confidence": float(r[4]),
            "importance": float(r[5]),
            "score": float(r[6]),
            "unique_source_count": int(r[7] or 0),
            "source_user_id": r[8],
        }
        for r in rows
    ]


def run_once() -> int:
    """Run one trigger pass. Returns count of drafts generated."""
    clusters = find_qualifying_clusters()
    if not clusters:
        print("No qualifying atoms found.")
        return 0

    generated = 0
    for atom in clusters:
        author = atom["source_user_id"] or "local_user"
        result = generate_draft(atom_ids=[atom["id"]], author_username=author)
        if result:
            print(
                f"Draft created: [{result['format']}] {result['title']!r} "
                f"(atom {atom['id'][:8]})"
            )
            generated += 1
        else:
            print(f"  skip {atom['id'][:8]} — generation failed")

    return generated


def main() -> None:
    parser = argparse.ArgumentParser(description="Memory layer publish trigger")
    parser.add_argument("--loop", action="store_true", help="Run continuously every 15 min")
    parser.add_argument("--interval", type=int, default=900, help="Loop interval in seconds")
    args = parser.parse_args()

    if args.loop:
        print(f"Publish trigger running (interval={args.interval}s). Ctrl-C to stop.")
        while True:
            try:
                n = run_once()
                print(f"Pass complete: {n} draft(s) generated.")
            except Exception as exc:
                print(f"Pass error: {exc}")
            time.sleep(args.interval)
    else:
        n = run_once()
        print(f"{n} draft(s) generated.")


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    main()
