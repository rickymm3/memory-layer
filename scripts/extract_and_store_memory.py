#!/usr/bin/env python3
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.memory_store import MemoryStore
from app.llm_provider import get_llm_client
from app.reconciliation import CandidateReconciler
from scripts.extract_memory_candidates import extract_candidates


def _coerce_float(value, default: float) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _prompt_confirm(prompt: str) -> bool:
    while True:
        answer = input(prompt).strip().lower()
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no", ""}:
            return False
        print("Please answer y or n.")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Extract memory candidates and interactively store accepted ones."
    )
    parser.add_argument("text", nargs="?", help="Conversation text (or use stdin)")
    parser.add_argument(
        "--dupe-threshold",
        type=float,
        default=0.93,
        help="Similarity threshold for duplicate check (0-1, default 0.93)",
    )
    parser.add_argument(
        "--dupe-limit",
        type=int,
        default=3,
        help="How many nearest memories to check for duplicates (default 3)",
    )
    parser.add_argument(
        "--reconcile-limit",
        type=int,
        default=5,
        help="How many related memories to retrieve for reconciliation (default 5)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Show reconciliation internals including matched IDs and raw JSON",
    )
    args = parser.parse_args()

    conversation = args.text if args.text else sys.stdin.read()
    if not conversation.strip():
        print("[ERROR] No conversation text provided.", file=sys.stderr)
        return 1

    try:
        extracted = extract_candidates(conversation, include_rejected=False)
    except Exception as exc:
        print("[ERROR] Extraction failed.", file=sys.stderr)
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    candidates = extracted.get("candidates", [])
    if not isinstance(candidates, list):
        print("[ERROR] Extractor returned invalid candidates payload.", file=sys.stderr)
        return 2

    if not candidates:
        print(json.dumps({"candidates": []}, indent=2, ensure_ascii=False))
        print("No candidates marked for storage.")
        return 0

    print(json.dumps({"candidates": candidates}, indent=2, ensure_ascii=False))
    print()

    store = MemoryStore()
    store.init_db()
    reconciler = CandidateReconciler(store=store, ollama=get_llm_client())

    stored_count = 0
    skipped_count = 0

    prepared: list[dict] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            skipped_count += 1
            continue

        content = str(candidate.get("content", "")).strip()
        if not content:
            print("Skipping invalid candidate: missing content")
            skipped_count += 1
            continue

        summary_val = candidate.get("context_summary")
        context_summary = (
            str(summary_val).strip() if summary_val is not None else ""
        )
        if not context_summary:
            context_summary = content

        memory_type = str(candidate.get("memory_type", "fact") or "fact")
        scope_val = candidate.get("scope")
        scope = str(scope_val).strip() if scope_val is not None else None
        if scope == "":
            scope = None
        confidence = _coerce_float(candidate.get("confidence"), 0.8)
        importance = _coerce_float(candidate.get("importance"), 0.5)
        reason = str(candidate.get("reason", "")).strip()

        prepared.append(
            {
                "content": content,
                "context_summary": context_summary,
                "memory_type": memory_type,
                "scope": scope,
                "confidence": confidence,
                "importance": importance,
                "reason": reason,
            }
        )

    skipped_duplicates: list[dict] = []
    skipped_reinforcement: list[dict] = []
    storable: list[dict] = []
    for item in prepared:
        reconciliation = reconciler.reconcile(
            content=item["content"],
            memory_type=item["memory_type"],
            scope=item["scope"],
            retrieve_limit=args.reconcile_limit,
        )
        relationship = reconciliation.get("relationship", "new")

        if relationship == "duplicate":
            skipped_duplicates.append({"candidate": item, "reconciliation": reconciliation})
            continue
        if relationship == "reinforcement":
            skipped_reinforcement.append({"candidate": item, "reconciliation": reconciliation})
            continue

        if relationship in {"refinement", "conflict", "opinion_change", "new"}:
            storable.append({"candidate": item, "reconciliation": reconciliation})
        else:
            storable.append(
                {
                    "candidate": item,
                    "reconciliation": {
                        "relationship": "new",
                        "matched_memory_ids": [],
                        "reason": "Unknown relationship from reconciler; treated as new.",
                        "recommended_action": "ask_user",
                    },
                }
            )

    if not storable:
        print("No new storable memory candidates from this exchange.")

    if skipped_duplicates:
        print("Duplicate memory candidate skipped:")
        for entry in skipped_duplicates:
            print(f"- {entry['candidate']['content']}")
            print(f"  reason: {entry['reconciliation'].get('reason', '')}")
            if args.debug:
                print(f"  [DEBUG] matched_ids={entry['reconciliation'].get('matched_memory_ids', [])}")
                print(f"  [DEBUG] reconciliation={entry['reconciliation'].get('raw_json')}")

    if skipped_reinforcement:
        print("Reinforcement candidate skipped for now:")
        for entry in skipped_reinforcement:
            print(f"- {entry['candidate']['content']}")
            print(f"  reason: {entry['reconciliation'].get('reason', '')}")
            if args.debug:
                print(f"  [DEBUG] matched_ids={entry['reconciliation'].get('matched_memory_ids', [])}")
                print(f"  [DEBUG] reconciliation={entry['reconciliation'].get('raw_json')}")

    if not storable:
        print(
            f"Done. Stored={stored_count}, "
            f"Skipped={skipped_count + len(skipped_duplicates) + len(skipped_reinforcement)}"
        )
        return 0

    print("Storable memory candidates:")
    for idx, entry in enumerate(storable, start=1):
        candidate = entry["candidate"]
        reconciliation = entry["reconciliation"]
        content = candidate["content"]
        context_summary = candidate["context_summary"]
        memory_type = candidate["memory_type"]
        scope = candidate["scope"]
        confidence = candidate["confidence"]
        importance = candidate["importance"]
        reason = candidate["reason"]
        relationship = reconciliation.get("relationship", "new")
        reconciliation_reason = reconciliation.get("reason", "")

        print(f"Candidate {idx}/{len(storable)}")
        print(f"- content: {content}")
        print(f"- context_summary: {context_summary}")
        print(f"- type: {memory_type}")
        print(f"- scope: {scope or 'global'}")
        print(f"- relationship: {relationship}")
        if reconciliation_reason:
            print(f"- reconciliation_reason: {reconciliation_reason}")
        print(f"- confidence: {confidence:.3f}")
        print(f"- importance: {importance:.3f}")
        if reason:
            print(f"- reason: {reason}")
        if args.debug:
            print(f"- [DEBUG] matched_memory_ids: {reconciliation.get('matched_memory_ids', [])}")
            print(f"- [DEBUG] reconciliation_json: {reconciliation.get('raw_json')}")

        if relationship in {"conflict", "opinion_change"}:
            print("Potential memory conflict detected. Review carefully before storing.")

        if not _prompt_confirm("Store this candidate? [y/N]: "):
            print("Skipped by user.")
            print()
            skipped_count += 1
            continue

        exact_match = store.find_exact_content_match(content)
        if exact_match:
            print(f"Not stored due to exact duplicate: existing_id={exact_match['id']}")
            print()
            skipped_count += 1
            continue

        signal_metadata: dict | None = None
        matched_ids = reconciliation.get("matched_memory_ids", [])
        recommended = reconciliation.get("recommended_action")
        if matched_ids or recommended:
            signal_metadata = {
                "matched_memory_ids": matched_ids,
                "recommended_action": recommended,
            }

        atom_id, _signal_id = store.store_memory_with_signal(
            content=content,
            context_summary=context_summary,
            memory_type=memory_type,
            scope=scope,
            confidence=confidence,
            importance=importance,
            relationship=relationship,
            reconciliation_reason=reconciliation_reason or None,
            raw_input=conversation,
            signal_metadata=signal_metadata,
        )

        print(f"Stored memory ID: {atom_id}")
        print()
        stored_count += 1

    print(f"Done. Stored={stored_count}, Skipped={skipped_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
