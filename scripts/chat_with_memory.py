#!/usr/bin/env python3
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.chat import chat_with_memory, clean_assistant_response
from app.memory_store import MemoryStore


def main() -> int:
    parser = argparse.ArgumentParser(description="Chat with retrieved memory context.")
    parser.add_argument("prompt", help="User prompt")
    parser.add_argument("--limit", type=int, default=5, help="Retrieved memory count")
    parser.add_argument("--debug", action="store_true", help="Show raw assistant output")
    args = parser.parse_args()

    store = MemoryStore()
    store.init_db()

    answer, memories = chat_with_memory(args.prompt, limit=args.limit)

    print("Retrieved memories:")
    if not memories:
        print("- (none)")
    else:
        for memory in memories:
            scope_value = memory["scope"] or "global"
            injected_text = memory.get("context_summary") or memory["content"]
            print(
                f"- [{memory['memory_type']}/{scope_value}/{memory['similarity']:.3f}] "
                f"{memory['content']}"
            )
            print(f"  summary_for_prompt: {injected_text}")

    print()
    print("Assistant answer:")
    print(answer if args.debug else clean_assistant_response(answer))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
