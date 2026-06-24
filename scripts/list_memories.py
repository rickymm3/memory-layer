#!/usr/bin/env python3
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.memory_store import MemoryStore


def main() -> int:
    parser = argparse.ArgumentParser(description="List recent memories.")
    parser.add_argument("--scope", default=None, help="Filter by scope")
    parser.add_argument("--type", dest="memory_type", default=None, help="Filter by memory_type")
    parser.add_argument("--limit", type=int, default=20, help="Max rows to return")
    args = parser.parse_args()

    if args.limit <= 0:
        print("[ERROR] --limit must be > 0", file=sys.stderr)
        return 1

    store = MemoryStore()
    store.init_db()

    memories = store.list_memories(
        scope=args.scope,
        memory_type=args.memory_type,
        limit=args.limit,
    )

    if not memories:
        print("No memories found.")
        return 0

    for memory in memories:
        usc = memory.get("unique_source_count", 0)
        sources_tag = f"  [{usc} source{'s' if usc != 1 else ''}]" if usc else ""
        print(f"id: {memory['id']}")
        print(f"type: {memory['memory_type']}  scope: {memory['scope']}{sources_tag}")
        print(f"content: {memory['content']}")
        if memory.get("context_summary"):
            print(f"inject: {memory['context_summary']}")
        print(f"created: {memory['created_at']}")
        print("-" * 60)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
