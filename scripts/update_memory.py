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
    parser = argparse.ArgumentParser(description="Update a memory by id with confirmation.")
    parser.add_argument("id", help="Memory ID (UUID)")
    parser.add_argument("--content", default=argparse.SUPPRESS)
    parser.add_argument("--summary", dest="context_summary", default=argparse.SUPPRESS)
    parser.add_argument("--type", dest="memory_type", default=argparse.SUPPRESS)
    parser.add_argument("--scope", default=argparse.SUPPRESS)
    parser.add_argument("--confidence", type=float, default=argparse.SUPPRESS)
    parser.add_argument("--importance", type=float, default=argparse.SUPPRESS)
    args = parser.parse_args()

    store = MemoryStore()
    store.init_db()

    try:
        existing = store.get_memory(args.id)
    except Exception as exc:
        print(f"[ERROR] Failed to load memory: {exc}", file=sys.stderr)
        return 1

    if not existing:
        print(f"[FAIL] Memory not found: {args.id}")
        return 1

    print("Existing memory:")
    _print_memory(existing)
    print()

    updates = {}
    for key in ["content", "context_summary", "memory_type", "scope", "confidence", "importance"]:
        if hasattr(args, key):
            updates[key] = getattr(args, key)

    if not updates:
        print("[FAIL] No update flags provided. Use one or more of --content --summary --type --scope --confidence --importance")
        return 1

    print("Proposed updates:")
    for key, value in updates.items():
        print(f"- {key}: {existing.get(key)} -> {value}")
    print()

    if not _confirm("Apply these changes? [y/N]: "):
        print("[CANCELLED] Memory was not updated.")
        return 0

    try:
        updated, embedding_regenerated = store.update_memory(args.id, updates)
    except Exception as exc:
        print(f"[ERROR] Failed to update memory: {exc}", file=sys.stderr)
        return 1

    if not updated:
        print(f"[FAIL] Memory was not updated: {args.id}")
        return 1

    if embedding_regenerated:
        print(f"[SUCCESS] Updated memory: {args.id} (embedding regenerated from new content)")
    else:
        print(f"[SUCCESS] Updated memory: {args.id} (embedding unchanged)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
