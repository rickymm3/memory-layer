#!/usr/bin/env python3
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.memory_store import MemoryStore

def main() -> int:
    parser = argparse.ArgumentParser(description="Store a cleaned memory atom.")
    parser.add_argument("content", help="Memory atom content")
    parser.add_argument("--summary", default=None, help="Optional concise context summary")
    parser.add_argument("--type", default="fact", dest="memory_type", help="Memory type")
    parser.add_argument("--scope", default=None, help="Optional memory scope")
    parser.add_argument("--confidence", type=float, default=0.8, help="Confidence 0-1")
    parser.add_argument("--importance", type=float, default=0.5, help="Importance 0-1")
    parser.add_argument("--dupe-threshold", type=float, default=0.93, help="Similarity threshold for duplicate check (0-1, default 0.93)")
    parser.add_argument("--dupe-limit", type=int, default=3, help="How many nearest memories to check for duplicates (default 3)")
    args = parser.parse_args()

    store = MemoryStore()
    store.init_db()

    memory_id, duplicates = store.store_memory_if_not_duplicate(
        content=args.content,
        context_summary=args.summary,
        memory_type=args.memory_type,
        scope=args.scope,
        confidence=args.confidence,
        importance=args.importance,
        dupe_threshold=args.dupe_threshold,
        dupe_limit=args.dupe_limit,
    )

    if duplicates:
        top = duplicates[0]
        if top.get("context_summary_updated"):
            print(
                "Updated existing memory row from near-duplicate "
                f"(id={top['id']}) with new context_summary."
            )
            return 0

        print(
            f"[WARN] Not storing: near-duplicate found "
            f"(similarity={top['similarity']:.3f} >= threshold={args.dupe_threshold})"
        )
        print(f"Existing memory id={top['id']}: {top['content']}")
        return 1

    print(f"Stored memory ID: {memory_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
