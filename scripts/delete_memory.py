#!/usr/bin/env python3
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.memory_store import MemoryStore


def _print_memory(memory: dict) -> None:
    print(f"id: {memory['id']}")
    print(f"memory_type: {memory['memory_type']}")
    print(f"scope: {memory['scope']}")
    print(f"content: {memory['content']}")
    print(f"context_summary: {memory['context_summary']}")
    print(f"confidence: {memory['confidence']}")
    print(f"importance: {memory['importance']}")
    print(f"created_at: {memory['created_at']}")


def _confirm(prompt: str) -> bool:
    answer = input(prompt).strip().lower()
    return answer in {"y", "yes"}


def main() -> int:
    parser = argparse.ArgumentParser(description="Delete a memory by id with confirmation.")
    parser.add_argument("id", help="Memory ID (UUID)")
    args = parser.parse_args()

    store = MemoryStore()
    store.init_db()

    try:
        memory = store.get_memory(args.id)
    except Exception as exc:
        print(f"[ERROR] Failed to load memory: {exc}", file=sys.stderr)
        return 1

    if not memory:
        print(f"[FAIL] Memory not found: {args.id}")
        return 1

    print("Memory to delete:")
    _print_memory(memory)
    print()

    if not _confirm("Delete this memory? [y/N]: "):
        print("[CANCELLED] Memory was not deleted.")
        return 0

    try:
        deleted = store.delete_memory(args.id)
    except Exception as exc:
        print(f"[ERROR] Failed to delete memory: {exc}", file=sys.stderr)
        return 1

    if deleted:
        print(f"[SUCCESS] Deleted memory: {args.id}")
        return 0

    print(f"[FAIL] Memory was not deleted: {args.id}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
