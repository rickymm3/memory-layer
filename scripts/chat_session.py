#!/usr/bin/env python3
import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.chat import build_prompt, clean_assistant_response
from app.memory_store import MemoryStore
from app.llm_provider import get_llm_client
from app.reconciliation import CandidateReconciler
from scripts.extract_memory_candidates import extract_candidates


HELP_TEXT = """Commands:
  /help        Show this help message
  /memories    List recent memories
  /quit        Exit session
  /exit        Exit session
"""


def _coerce_float(value, default: float) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _confirm(prompt: str) -> bool:
    answer = input(prompt).strip().lower()
    return answer in {"y", "yes"}


def _is_question_only_message(user_message: str) -> bool:
    text = user_message.strip()
    if not text.endswith("?"):
        return False

    lowered = text.lower()
    directive_markers = (
        "please remember",
        "remember that",
        "note that",
        "we decided",
        "i prefer",
        "for now",
        "do not",
        "don't",
        "must",
        "need to",
    )
    return not any(marker in lowered for marker in directive_markers)


def _sanitize_commitment_language(text: str) -> str:
    sanitized = text
    replacements = [
        (r"\bhas been noted\b", "has been flagged for review"),
        (r"\bwill be implemented\b", "can be reviewed before any change"),
        (r"\bI(?:'| a)m going to implement\b", "I can propose this for review"),
        (r"\bI(?:'| a)m implementing\b", "I am proposing this for review"),
    ]
    for pattern, replacement in replacements:
        sanitized = re.sub(pattern, replacement, sanitized, flags=re.IGNORECASE)
    return sanitized


def _clean_user_reason(text: str) -> str:
    cleaned = re.sub(r"\[[^\]]+/[^\]]+\]\s*", "", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def _prepare_candidates(candidates: list[dict]) -> list[dict]:
    prepared: list[dict] = []
    for candidate in candidates:
        content = str(candidate.get("content", "")).strip()
        if not content:
            continue

        summary_val = candidate.get("context_summary")
        context_summary = str(summary_val).strip() if summary_val is not None else ""
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

    return prepared


def _reconcile_candidates(
    reconciler: CandidateReconciler,
    prepared_candidates: list[dict],
    reconcile_limit: int,
) -> dict[str, list[dict]]:
    skipped_duplicates: list[dict] = []
    skipped_reinforcement: list[dict] = []
    storable: list[dict] = []

    for item in prepared_candidates:
        reconciliation = reconciler.reconcile(
            content=item["content"],
            memory_type=item["memory_type"],
            scope=item["scope"],
            retrieve_limit=reconcile_limit,
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
            continue

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

    return {
        "skipped_duplicates": skipped_duplicates,
        "skipped_reinforcement": skipped_reinforcement,
        "storable": storable,
    }


def _response_for_reconciliation_state(
    base_response: str,
    reconciled: dict[str, list[dict]] | None,
) -> str:
    sanitized = _sanitize_commitment_language(base_response)
    if reconciled is None:
        return sanitized

    storable = reconciled.get("storable", [])
    has_conflict = any(
        entry.get("reconciliation", {}).get("relationship") in {"conflict", "opinion_change"}
        for entry in storable
    )
    has_reviewable = any(
        entry.get("reconciliation", {}).get("relationship") in {"new", "refinement"}
        for entry in storable
    )

    if has_conflict:
        return (
            "That conflicts with a current stored memory. "
            "I will flag it for review before storing anything."
        )
    if has_reviewable:
        return (
            "Understood. I can propose this for review, and no memory write "
            "or implementation happens without confirmation."
        )
    return sanitized


def _print_recent_memories(store: MemoryStore, limit: int) -> None:
    memories = store.list_memories(limit=limit)
    if not memories:
        print("No memories found.")
        return

    print(f"Recent memories (limit={limit}):")
    for memory in memories:
        print(f"- id: {memory['id']}")
        print(f"  type: {memory['memory_type']}")
        print(f"  scope: {memory['scope']}")
        print(f"  content: {memory['content']}")
        print(f"  context_summary: {memory['context_summary']}")
        print(f"  created_at: {memory['created_at']}")


def _build_signal_metadata(reconciliation: dict) -> dict | None:
    matched_ids = reconciliation.get("matched_memory_ids", [])
    recommended = reconciliation.get("recommended_action")
    if matched_ids or recommended:
        return {"matched_memory_ids": matched_ids, "recommended_action": recommended}
    return None


def _is_auto_storable(entry: dict) -> bool:
    """Return True for safe low-risk new/refinement candidates the write policy
    should store without prompting."""
    relationship = entry["reconciliation"].get("relationship", "new")
    recommended = entry["reconciliation"].get("recommended_action", "ask_user")
    return relationship in {"new", "refinement"} and recommended == "store_new"


def _print_write_report(
    atom_id: str,
    signal_id: str,
    candidate: dict,
    relationship: str,
    auto_stored: bool,
) -> None:
    scope = candidate.get("scope") or "global"
    print("[AUTO-STORED]")
    print(f"  memory_atom_id:   {atom_id}")
    print(f"  memory_signal_id: {signal_id}")
    print(f"  content:          {candidate['content']}")
    print(f"  type:             {candidate['memory_type']}")
    print(f"  scope:            {scope}")
    print(f"  relationship:     {relationship}")
    print(f"  signal_created:   true")
    print(f"  auto_stored:      {str(auto_stored).lower()}")


def _review_and_store_candidates(
    store: MemoryStore,
    reconciled: dict[str, list[dict]],
    dupe_threshold: float,
    dupe_limit: int,
    debug: bool,
    raw_input: str | None = None,
) -> None:
    skipped_duplicates = reconciled.get("skipped_duplicates", [])
    skipped_reinforcement = reconciled.get("skipped_reinforcement", [])
    storable = reconciled.get("storable", [])

    if not skipped_duplicates and not skipped_reinforcement and not storable:
        print("No storable memory candidates from this exchange.")
        return

    if not storable:
        print("No new storable memory candidates from this exchange.")

    if skipped_duplicates:
        print("Duplicate memory candidate skipped:")
        for entry in skipped_duplicates:
            print(f"- {entry['candidate']['content']}")
            print(f"  reason: {entry['reconciliation'].get('reason', '')}")
            if debug:
                print(f"  [DEBUG] reconciliation={entry['reconciliation'].get('raw_json')} matched_ids={entry['reconciliation'].get('matched_memory_ids', [])}")

    if skipped_reinforcement:
        print("Reinforcement candidate skipped for now:")
        for entry in skipped_reinforcement:
            print(f"- {entry['candidate']['content']}")
            print(f"  reason: {entry['reconciliation'].get('reason', '')}")
            if debug:
                print(f"  [DEBUG] reconciliation={entry['reconciliation'].get('raw_json')} matched_ids={entry['reconciliation'].get('matched_memory_ids', [])}")

    if not storable:
        return

    # Split by write policy: auto-store safe low-risk candidates; prompt for the rest
    auto_storable = [e for e in storable if _is_auto_storable(e)]
    confirm_required = [e for e in storable if not _is_auto_storable(e)]

    for entry in auto_storable:
        candidate = entry["candidate"]
        reconciliation = entry["reconciliation"]
        relationship = reconciliation.get("relationship", "new")
        reconciliation_reason = reconciliation.get("reason", "")
        if isinstance(reconciliation_reason, str):
            reconciliation_reason = _clean_user_reason(reconciliation_reason)

        exact_match = store.find_exact_content_match(candidate["content"])
        if exact_match:
            print(f"Not stored (exact duplicate at write time): existing_id={exact_match['id']}")
            continue

        atom_id, signal_id = store.store_memory_with_signal(
            content=candidate["content"],
            context_summary=candidate["context_summary"],
            memory_type=candidate["memory_type"],
            scope=candidate["scope"],
            confidence=candidate["confidence"],
            importance=candidate["importance"],
            relationship=relationship,
            reconciliation_reason=reconciliation_reason or None,
            raw_input=raw_input,
            signal_metadata=_build_signal_metadata(reconciliation),
        )
        _print_write_report(
            atom_id=atom_id,
            signal_id=signal_id,
            candidate=candidate,
            relationship=relationship,
            auto_stored=True,
        )

    if not confirm_required:
        return

    print("Storable memory candidates (require confirmation):")
    for idx, entry in enumerate(confirm_required, start=1):
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
        if isinstance(reconciliation_reason, str):
            reconciliation_reason = _clean_user_reason(reconciliation_reason)
        if isinstance(reason, str):
            reason = _clean_user_reason(reason)

        print(f"Candidate {idx}/{len(confirm_required)}")
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
        if debug:
            print(f"- [DEBUG] matched_memory_ids: {reconciliation.get('matched_memory_ids', [])}")
            print(f"- [DEBUG] reconciliation_json: {reconciliation.get('raw_json')}")

        if relationship in {"conflict", "opinion_change"}:
            print("Potential memory conflict detected. Review carefully before storing.")

        if not _confirm("Store this candidate? [y/N]: "):
            print("Skipped by user.")
            continue

        exact_match = store.find_exact_content_match(content)
        if exact_match:
            print(f"Not stored due to exact duplicate: existing_id={exact_match['id']}")
            continue

        signal_metadata: dict | None = _build_signal_metadata(reconciliation)

        atom_id, signal_id = store.store_memory_with_signal(
            content=content,
            context_summary=context_summary,
            memory_type=memory_type,
            scope=scope,
            confidence=confidence,
            importance=importance,
            relationship=relationship,
            reconciliation_reason=reconciliation_reason or None,
            raw_input=raw_input,
            signal_metadata=signal_metadata,
        )
        _print_write_report(
            atom_id=atom_id,
            signal_id=signal_id,
            candidate=entry["candidate"],
            relationship=relationship,
            auto_stored=False,
        )


def _build_extraction_input(user_message: str, assistant_answer: str, memories: list[dict]) -> str:
    retrieved_lines: list[str] = []
    for memory in memories:
        scope = memory.get("scope") or "global"
        text = memory.get("context_summary") or memory.get("content") or ""
        retrieved_lines.append(f"- [{memory.get('memory_type')}/{scope}] {text}")

    retrieved_block = "\n".join(retrieved_lines) if retrieved_lines else "- (none)"

    return (
        "Extraction context:\n"
        "Only the user message is eligible for memory extraction.\n"
        "Assistant answer and retrieved memories are context-only and not source of truth.\n\n"
        "<user_message>\n"
        f"{user_message}\n"
        "</user_message>\n\n"
        "<assistant_answer_context_only>\n"
        f"{assistant_answer}\n"
        "</assistant_answer_context_only>\n\n"
        "<retrieved_memories_context_only>\n"
        f"{retrieved_block}\n"
        "</retrieved_memories_context_only>\n"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Interactive chat session with memory retrieval and manual memory storage.")
    parser.add_argument("--retrieve-limit", type=int, default=5, help="How many memories to retrieve per turn")
    parser.add_argument("--list-limit", type=int, default=10, help="How many memories to show for /memories")
    parser.add_argument("--dupe-threshold", type=float, default=0.93, help="Duplicate threshold for final write-time guard")
    parser.add_argument("--dupe-limit", type=int, default=3, help="How many nearest memories to compare for final write-time guard")
    parser.add_argument("--reconcile-limit", type=int, default=5, help="How many related memories to retrieve for reconciliation")
    parser.add_argument("--debug", action="store_true", help="Show raw assistant output and prompt metadata")
    args = parser.parse_args()

    store = MemoryStore()
    store.init_db()
    reconciler = CandidateReconciler(store=store, ollama=get_llm_client())

    print("Memory chat session started. Type /help for commands.")

    while True:
        try:
            user_message = input("\nYou: ").strip()
        except EOFError:
            print("\nExiting.")
            return 0
        except KeyboardInterrupt:
            print("\nExiting.")
            return 0

        if not user_message:
            continue

        if user_message in {"/exit", "/quit"}:
            print("Exiting.")
            return 0

        if user_message == "/help":
            print(HELP_TEXT)
            continue

        if user_message == "/memories":
            _print_recent_memories(store, limit=args.list_limit)
            continue

        try:
            memories = store.retrieve_memories(user_message, limit=args.retrieve_limit)
            grounded_prompt = build_prompt(user_message, memories)
            answer = ollama.generate_response(grounded_prompt)
        except Exception as exc:
            print(f"[ERROR] Chat failed: {exc}", file=sys.stderr)
            continue

        if _is_question_only_message(user_message):
            display_answer = _response_for_reconciliation_state(
                clean_assistant_response(answer),
                None,
            )
            print(f"Assistant: {display_answer}")
            if args.debug:
                print(f"[DEBUG] Raw assistant output: {answer}")
                print("[DEBUG] Retrieved memories used in prompt:")
                for memory in memories:
                    scope = memory.get("scope") or "global"
                    sim = memory.get("similarity", 0.0)
                    text = memory.get("context_summary") or memory.get("content")
                    print(f"  - [{memory.get('memory_type')}/{scope}/{sim:.3f}] {text}")
            print("No storable memory candidates from this exchange.")
            continue

        extraction_text = _build_extraction_input(user_message, answer, memories)
        reconciled: dict[str, list[dict]] | None = None
        try:
            extraction = extract_candidates(extraction_text, include_rejected=False)
            candidates = extraction.get("candidates", [])
            if not isinstance(candidates, list):
                raise ValueError("extractor returned non-list candidates")
            prepared = _prepare_candidates(candidates)
            reconciled = _reconcile_candidates(
                reconciler=reconciler,
                prepared_candidates=prepared,
                reconcile_limit=args.reconcile_limit,
            )
            display_answer = _response_for_reconciliation_state(
                clean_assistant_response(answer),
                reconciled,
            )
            print(f"Assistant: {display_answer}")
            if args.debug:
                print(f"[DEBUG] Raw assistant output: {answer}")
                print("[DEBUG] Retrieved memories used in prompt:")
                for memory in memories:
                    scope = memory.get("scope") or "global"
                    sim = memory.get("similarity", 0.0)
                    text = memory.get("context_summary") or memory.get("content")
                    print(f"  - [{memory.get('memory_type')}/{scope}/{sim:.3f}] {text}")
            _review_and_store_candidates(
                store=store,
                reconciled=reconciled,
                dupe_threshold=args.dupe_threshold,
                dupe_limit=args.dupe_limit,
                debug=args.debug,
                raw_input=user_message,
            )
        except Exception as exc:
            display_answer = _response_for_reconciliation_state(
                clean_assistant_response(answer),
                reconciled,
            )
            print(f"Assistant: {display_answer}")
            if args.debug:
                print(f"[DEBUG] Raw assistant output: {answer}")
                print("[DEBUG] Retrieved memories used in prompt:")
                for memory in memories:
                    scope = memory.get("scope") or "global"
                    sim = memory.get("similarity", 0.0)
                    text = memory.get("context_summary") or memory.get("content")
                    print(f"  - [{memory.get('memory_type')}/{scope}/{sim:.3f}] {text}")
            print(f"[WARN] Candidate extraction/review skipped: {exc}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
