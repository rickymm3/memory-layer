#!/usr/bin/env python3
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.memory_store import MemoryStore


def main() -> int:
    parser = argparse.ArgumentParser(description="Retrieve nearest memories by similarity.")
    parser.add_argument("query", help="Query text")
    parser.add_argument("--limit", type=int, default=5, help="Number of memories")
    args = parser.parse_args()

    store = MemoryStore()
    store.init_db()
    memories = store.retrieve_memories(args.query, limit=args.limit)

    if not memories:
        print("No memories found.")
        return 0

    for i, memory in enumerate(memories, start=1):
        scope_value = memory["scope"] or "global"
        print(
            f"{i}. id={memory['id']} sim={memory['similarity']:.4f} "
            f"type={memory['memory_type']} scope={scope_value}"
        )
        print(f"   {memory['content']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
