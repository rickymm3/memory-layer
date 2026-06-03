#!/usr/bin/env python3
"""End-of-task reflection for the memory-layer project.

Extracts concrete lessons from a completed implementation task and optionally
stores safe, low-risk ones via the existing write policy (store_memory_auto path).

Every stored lesson creates one memory_atom + one linked memory_signal in a
single transaction. The write report prints both IDs.

Usage (dry-run, default):
    python scripts/reflect_task.py \\
        --scope project:memory-layer \\
        --task "Added /scopes dashboard route" \\
        --files "dashboard/app.py dashboard/templates/scopes.html" \\
        --tests "make doctor: 30 PASS, 1 WARN, 0 FAIL; 6-route smoke test: all 200/404" \\
        --outcome success \\
        --notes "All SQL queries parameterized. Scope filter links go to /?scope=<slug>."

Usage (write safe lessons via write policy):
    python scripts/reflect_task.py ... --store
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg

from app.config import get_config
from app.llm_provider import get_llm_client
from mcp_server.tools.store_auto import store_memory_auto


_REFLECTION_PROMPT = """\
You are a task-reflection assistant for a software project that uses a persistent memory layer.

Your job is to extract concrete, reusable lessons from a completed implementation task.

A lesson is worth storing if it is:
- A specific, observed behavior of a model, tool, or project workflow
- An architectural constraint or decision relevant to future work in this scope
- A workflow guardrail that prevented or caught a mistake
- A concrete instruction about what a model or tool requires to work correctly

Do NOT generate lessons that are:
- Vague or generic ("make sure to test things", "always write good code")
- Speculative about what might happen in the future
- Simply restating the task description or the project's known invariants
- Covered already by obvious engineering best practice

Classification rules:
- safe_to_store: concrete, low-risk, observed fact or instruction; safe to auto-store
- needs_review: uncertain, potentially conflicting, policy-related, or high-stakes
- skip: too vague, speculative, generic, or already obvious

For each lesson, return:
- content: a full, standalone sentence (canonical memory atom)
- context_summary: a concise, prompt-friendly version (may omit scope qualifier if meaning is clear)
- memory_type: one of "instruction", "fact", or "decision"
- scope: the exact scope this applies to (e.g. "model:qwen3-8b", "project:memory-layer", "project:memory-layer-dashboard")
- classification: "safe_to_store" | "needs_review" | "skip"

Return a strict JSON array only. No preamble, no explanation, no markdown fences.

Example output:
[
  {{
    "content": "For project:memory-layer, all dashboard SQL queries must use parameterized inputs only.",
    "context_summary": "Dashboard SQL must always use parameterized inputs.",
    "memory_type": "instruction",
    "scope": "project:memory-layer-dashboard",
    "classification": "safe_to_store"
  }}
]

Task reflection context:
  Task    : {task}
  Scope   : {scope}
  Files   : {files}
  Tests   : {tests}
  Outcome : {outcome}
  Notes   : {notes}

Existing active lessons already stored in scope "{scope}" (do NOT re-store these;
classify any candidate that overlates with an existing lesson as "skip"):
{existing_lessons}
"""

_JSON_FENCE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)


def _fetch_existing_lessons(scope: str) -> str:
    """Return a compact list of active atoms in the scope for dedup context."""
    try:
        cfg = get_config()
        with psycopg.connect(cfg.database_url) as conn:
            rows = conn.execute(
                """
                SELECT content
                FROM memory_atoms
                WHERE scope = %s AND lifecycle_status = 'active'
                ORDER BY created_at
                """,
                (scope,),
            ).fetchall()
        if not rows:
            return "(none yet)"
        return "\n".join(f"- {r[0]}" for r in rows)
    except Exception:
        return "(could not fetch — proceed without dedup context)"


def _create_task_run(
    scope: str,
    task: str,
    files: str | None,
    tests: str | None,
    outcome: str,
    notes: str | None,
) -> str:
    """Insert a task_run record and return its UUID string."""
    cfg = get_config()
    with psycopg.connect(cfg.database_url) as conn:
        row = conn.execute(
            """
            INSERT INTO task_runs (scope, task_description, model_used, files_changed, test_results, outcome, notes)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id;
            """,
            (scope, task, cfg.chat_model, files, tests, outcome, notes),
        ).fetchone()
        conn.commit()
    return str(row[0])


def _update_task_run_lesson_count(task_run_id: str, count: int) -> None:
    """Set lessons_stored on a task_run record after the store loop completes."""
    cfg = get_config()
    with psycopg.connect(cfg.database_url) as conn:
        conn.execute(
            "UPDATE task_runs SET lessons_stored = %s WHERE id = %s",
            (count, task_run_id),
        )
        conn.commit()


_WIDTH = 62


def _parse_candidates(raw: str) -> list[dict]:
    """Extract JSON array from LLM output, tolerating markdown fences."""
    m = _JSON_FENCE.search(raw)
    text = m.group(1).strip() if m else raw.strip()
    start = text.find("[")
    if start != -1:
        text = text[start:]
    return json.loads(text)


def _sep(char: str = "─") -> None:
    print(char * _WIDTH)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "End-of-task reflection: extract and optionally store lessons. "
            "Dry-run by default; pass --store to write safe lessons via the write policy."
        )
    )
    parser.add_argument(
        "--scope",
        default="project:memory-layer",
        help="Project scope (default: project:memory-layer)",
    )
    parser.add_argument("--task", required=True, help="Short task description")
    parser.add_argument(
        "--files",
        default=None,
        help="Files changed (space- or comma-separated)",
    )
    parser.add_argument(
        "--tests",
        default=None,
        help="Tests run and their results",
    )
    parser.add_argument(
        "--outcome",
        choices=["success", "partial", "failed"],
        default="success",
        help="Task outcome (default: success)",
    )
    parser.add_argument(
        "--notes",
        default=None,
        help="Lessons, observations, or corrections from this task",
    )
    parser.add_argument(
        "--store",
        action="store_true",
        help="Store safe_to_store lessons via the write policy (default: dry-run only)",
    )
    args = parser.parse_args()

    # ── 1. Print structured reflection header ─────────────────────────────────
    _sep("═")
    print("TASK REFLECTION")
    _sep("═")
    print(f"  Scope  : {args.scope}")
    print(f"  Task   : {args.task}")
    print(f"  Files  : {args.files or '(not specified)'}")
    print(f"  Tests  : {args.tests or '(not specified)'}")
    print(f"  Outcome: {args.outcome.upper()}")
    print(f"  Notes  : {args.notes or '(none)'}")
    _sep()

    # ── 2. Call LLM to extract candidate lessons ───────────────────────────────
    print("Extracting candidate lessons via LLM…")
    existing_lessons = _fetch_existing_lessons(args.scope)
    try:
        llm = get_llm_client()
        prompt = _REFLECTION_PROMPT.format(
            task=args.task,
            scope=args.scope,
            files=args.files or "(not specified)",
            tests=args.tests or "(not specified)",
            outcome=args.outcome,
            notes=args.notes or "(none)",
            existing_lessons=existing_lessons,
        )
        raw = llm.generate_response(prompt)
    except Exception as exc:
        print(f"[ERROR] LLM call failed: {exc}", file=sys.stderr)
        return 1

    try:
        candidates = _parse_candidates(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"[ERROR] Could not parse LLM response as JSON: {exc}", file=sys.stderr)
        print(f"Raw LLM output:\n{raw}", file=sys.stderr)
        return 1

    if not isinstance(candidates, list):
        print("[ERROR] LLM returned non-list JSON", file=sys.stderr)
        return 1

    # ── 3. Print classified candidates ────────────────────────────────────────
    safe = [c for c in candidates if c.get("classification") == "safe_to_store"]
    review = [c for c in candidates if c.get("classification") == "needs_review"]
    skipped = [c for c in candidates if c.get("classification") == "skip"]

    print(f"\nCandidate lessons found: {len(candidates)}")
    print(f"  safe_to_store : {len(safe)}")
    print(f"  needs_review  : {len(review)}")
    print(f"  skip          : {len(skipped)}")
    _sep()

    if not candidates:
        print("No lessons extracted. Nothing to store.")
        return 0

    _LABELS = {
        "safe_to_store": "[SAFE  ]",
        "needs_review": "[REVIEW]",
        "skip": "[SKIP  ]",
    }
    for i, c in enumerate(candidates, 1):
        cls = c.get("classification", "unknown")
        label = _LABELS.get(cls, f"[{cls.upper()[:6]}]")
        print(
            f"\n{i}. {label}  type={c.get('memory_type', '?')}  "
            f"scope={c.get('scope', '?')}"
        )
        print(f"   Content : {c.get('content', '')}")
        summary = c.get("context_summary", "")
        if summary and summary != c.get("content", ""):
            print(f"   Summary : {summary}")

    _sep()

    # ── 4. Dry-run report ─────────────────────────────────────────────────────
    if not args.store:
        if safe:
            print(
                f"\nDRY-RUN: {len(safe)} safe lesson(s) would be stored. "
                "Re-run with --store to write."
            )
        if review:
            print(
                f"REVIEW : {len(review)} lesson(s) need manual review — "
                "inspect manually or use make review-proposals."
            )
        print("\nNo writes performed (dry-run mode).")
        return 0

    # ── 5. Store safe lessons via write policy ────────────────────────────────
    print(f"\nSTORE MODE: writing {len(safe)} safe lesson(s) via write policy…")
    stored_count = 0
    task_run_id = _create_task_run(
        scope=args.scope,
        task=args.task,
        files=args.files,
        tests=args.tests,
        outcome=args.outcome,
        notes=args.notes,
    )
    print(f"  [TASK RUN] id={task_run_id}")
    for c in safe:
        content = (c.get("content") or "").strip()
        if not content:
            print("  [SKIP] Empty content — skipping.")
            continue

        # Every write creates one memory_atom + one linked memory_signal.
        report = store_memory_auto(
            content=content,
            context_summary=c.get("context_summary") or None,
            memory_type=c.get("memory_type", "fact"),
            relationship="new",
            scope=c.get("scope") or args.scope,
            confidence=0.75,
            importance=0.7,
            reconciliation_reason="task_reflection",
            task_run_id=task_run_id,
        )

        if report.get("stored"):
            stored_count += 1
            print(f"\n  [STORED]")
            print(f"    memory_atom_id   : {report['memory_atom_id']}")
            print(f"    memory_signal_id : {report['memory_signal_id']}")
            print(f"    content          : {report['content']}")
            print(f"    type             : {report['memory_type']}")
            print(f"    scope            : {report['scope']}")
            print(f"    Creates          : memory_atom + linked memory_signal (single transaction)")
        else:
            err = report.get("error", "unknown error")
            existing = report.get("existing_memory_atom_id")
            if existing:
                print(f"  [SKIP] {err}  (existing_atom={existing})")
            else:
                print(f"  [SKIP] {err}")

    _sep()
    _update_task_run_lesson_count(task_run_id, stored_count)
    print(f"Done. {stored_count}/{len(safe)} safe lesson(s) stored.")
    if review:
        print(
            f"Note: {len(review)} needs_review lesson(s) were NOT stored. "
            "Inspect manually or use make review-proposals."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
