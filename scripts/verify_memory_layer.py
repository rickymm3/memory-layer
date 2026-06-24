#!/usr/bin/env python3
"""End-to-end verification of the memory layer learning pipeline.

Tests three layers:
  Layer 1: Write quality gate — bad content rejected/downgraded, good content accepted
  Layer 2: Injection pipeline — model lessons present and valid, hook output correct
  Layer 3: Behavioral signal — lessons align with known observed behaviors (heuristic)

Usage:
    python scripts/verify_memory_layer.py
    make verify

Exit code 0 = all checks pass. Non-zero = failures found.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv()

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
WARN = "\033[33mWARN\033[0m"


def _result(label: str, ok: bool, detail: str = "", warn: bool = False) -> bool:
    tag = WARN if (warn and not ok) else (PASS if ok else FAIL)
    line = f"  [{tag}] {label}"
    if detail:
        line += f" — {detail}"
    print(line)
    return ok


# ── Layer 1: Write quality gate ───────────────────────────────────────────────

def check_write_quality() -> int:
    """Returns number of failures.

    Epistemic philosophy: nothing is rejected for quality reasons.
    Guardrail blocks only: secrets, empty content, invalid scope.
    Everything else is stored with appropriate type and confidence.
    """
    from app.write_quality import score_write_quality, GUARDRAILS

    print("\nLayer 1: Epistemic classifier")
    failures = 0

    # ── Guardrail blocks: these MUST reject ───────────────────────────────────
    guardrail_cases = [
        ("", "fact", 0.5, None, "empty content → guardrail reject"),
        ("Use sk-ant-api03-secret123 for auth", "fact", 0.5, "user", "API key → guardrail reject"),
        ("password: hunter2secret!", "fact", 0.5, "user", "password field → guardrail reject"),
        ("We use PostgreSQL.", "fact", 0.5, "ai_training", "invalid scope → guardrail reject"),
    ]
    for content, mtype, imp, scope, label in guardrail_cases:
        r = score_write_quality(content, memory_type=mtype, stated_importance=imp, scope=scope)
        if not _result(label, r.decision == "reject", f"score={r.quality_score:.2f} decision={r.decision}"):
            failures += 1

    # ── Epistemic storage: these must NOT reject (stored at any tier) ─────────
    store_cases = [
        ("today I fixed a bug", "fact", 0.5, "project:memory-layer",
         "ephemeral date ref → stored (not rejected)", "observation"),
        ("todo: fix the tests next sprint", "fact", 0.5, "project:memory-layer",
         "todo/intent → stored as preference (not rejected)", "preference"),
        ("The sky is red", "fact", 0.5, "user",
         "user belief → stored as belief (not rejected)", "belief"),
        ("What database should we use?", "fact", 0.5, "project:memory-layer",
         "question → stored as observation (not rejected)", None),
        ("maybe it will work", "fact", 0.5, "user",
         "vague claim → stored as belief (not rejected)", "belief"),
    ]
    for content, mtype, imp, scope, label, expected_sugg_type in store_cases:
        r = score_write_quality(content, memory_type=mtype, stated_importance=imp, scope=scope)
        stored = r.decision != "reject"
        if not _result(label, stored, f"score={r.quality_score:.2f} decision={r.decision} suggested_type={r.suggested_memory_type}"):
            failures += 1
        elif expected_sugg_type and r.suggested_memory_type:
            type_ok = r.suggested_memory_type == expected_sugg_type
            if not _result(
                f"  └─ suggests {expected_sugg_type} type",
                type_ok,
                f"got {r.suggested_memory_type}",
                warn=True,
            ):
                pass  # soft warning only

    # ── Verified tier: high-quality content must accept ───────────────────────
    accept_cases = [
        (
            "Frame evaluations as adversarial audits, not health checks. "
            "Claude finds good news unless forced to look for problems.",
            "observation", 0.9, "model:claude-sonnet-4-6",
            "model lesson → VERIFIED tier (accept)",
        ),
        (
            "Require narrow task scope: only change what is asked, "
            "no surrounding refactoring or extra abstractions.",
            "observation", 0.85, "model:claude-sonnet-4-6",
            "model lesson → VERIFIED tier (accept)",
        ),
        (
            "Postgres is the primary database for memory-layer. SQLite is for tests only.",
            "decision", 0.8, "project:memory-layer",
            "tech decision → VERIFIED tier (accept)",
        ),
    ]
    for content, mtype, imp, scope, label in accept_cases:
        r = score_write_quality(content, memory_type=mtype, stated_importance=imp, scope=scope)
        if not _result(label, r.decision == "accept", f"score={r.quality_score:.2f} decision={r.decision}"):
            failures += 1

    # ── GUARDRAILS list is configurable ───────────────────────────────────────
    _result(f"GUARDRAILS list has {len(GUARDRAILS)} patterns (configurable)", True)

    return failures


# ── Layer 2: Injection pipeline ────────────────────────────────────────────────

def check_injection_pipeline(model_scope: str = "model:claude-sonnet-4-6") -> int:
    """Returns number of failures."""
    print(f"\nLayer 2: Injection pipeline ({model_scope})")
    failures = 0

    try:
        import psycopg
        database_url = os.environ.get("DATABASE_URL", "")
        if not database_url:
            _result("DB connection", False, "DATABASE_URL not set")
            return 1

        with psycopg.connect(database_url) as conn:
            rows = conn.execute(
                """
                SELECT id::text, context_summary, importance, confidence, lifecycle_status
                FROM memory_atoms
                WHERE scope = %s AND lifecycle_status = 'active'
                ORDER BY importance DESC
                """,
                (model_scope,),
            ).fetchall()

    except Exception as e:
        _result("DB connection", False, str(e))
        return 1

    # Check: at least some lessons present
    ok = len(rows) > 0
    if not _result(f"model lessons present ({len(rows)} atoms)", ok):
        return 1

    generic_prefixes = (
        "this observation", "this lesson", "this fact",
        "the following", "relevant for", "note that",
    )

    for atom_id, ctx_summary, imp, conf, status in rows:
        short_id = atom_id[:8]
        if not ctx_summary or len(ctx_summary.strip()) < 20:
            if not _result(f"{short_id} has injection string", False, "empty or too short"):
                failures += 1
            continue

        injection = ctx_summary.strip()
        is_generic = any(injection.lower().startswith(p) for p in generic_prefixes)
        if not _result(
            f"{short_id} injection is a directive",
            not is_generic,
            injection[:60],
        ):
            failures += 1

        # Importance should be >= 0.6 for model lessons
        imp_ok = float(imp) >= 0.6
        if not _result(f"{short_id} importance >= 0.6", imp_ok, f"imp={float(imp):.2f}", warn=True):
            failures += 1

    # Check: hook SQL would return the right content
    # (simulate what memory_search_inject.py does)
    try:
        with psycopg.connect(database_url) as conn:
            hook_rows = conn.execute(
                """
                SELECT COALESCE(context_summary, content), memory_type, confidence
                FROM memory_atoms
                WHERE scope = %s AND lifecycle_status = 'active'
                ORDER BY importance DESC, confidence DESC
                LIMIT 5
                """,
                (model_scope,),
            ).fetchall()

        hook_ok = len(hook_rows) > 0
        _result(
            f"hook query returns {len(hook_rows)} directives",
            hook_ok,
            "these are what appear in system-reminder",
        )
        if not hook_ok:
            failures += 1

        # Check hook output matches model-report output
        hook_injections = {r[0].strip() for r in hook_rows if r[0]}
        db_injections = {r[1].strip() for r in rows if r[1]} if rows else set()
        mismatch = hook_injections - db_injections
        if mismatch:
            _result(
                "hook output matches DB",
                False,
                f"{len(mismatch)} hook atoms not in DB active set",
                warn=True,
            )
        else:
            _result("hook output matches DB", True)

    except Exception as e:
        _result("hook simulation", False, str(e))
        failures += 1

    return failures


# ── Layer 3: Behavioral signal (heuristic) ────────────────────────────────────

def check_behavioral_signal(model_scope: str = "model:claude-sonnet-4-6") -> int:
    """Returns number of failures (WARN level — can't do A/B without API key)."""
    print("\nLayer 3: Behavioral signal (heuristic — no A/B test)")
    failures = 0

    # The four behaviors we want to be injecting:
    expected_behaviors = {
        "scope discipline": ["scope", "model:", "project:"],
        "adversarial framing": ["adversarial", "broken", "missing", "find what"],
        "narrow task scope": ["narrow", "only change", "refactor", "abstraction"],
        "CLAUDE.md enforcement": ["claude.md", "behavioral constraint", "degrade"],
    }

    try:
        import psycopg
        database_url = os.environ.get("DATABASE_URL", "")
        with psycopg.connect(database_url) as conn:
            rows = conn.execute(
                "SELECT context_summary FROM memory_atoms WHERE scope = %s AND lifecycle_status = 'active'",
                (model_scope,),
            ).fetchall()
    except Exception as e:
        _result("DB access for behavioral check", False, str(e))
        return 1

    all_injections = " ".join((r[0] or "").lower() for r in rows)

    for behavior, keywords in expected_behaviors.items():
        covered = any(kw in all_injections for kw in keywords)
        if not _result(
            f"'{behavior}' behavior covered by injection",
            covered,
            f"keywords: {keywords}",
            warn=True,
        ):
            failures += 1

    # Note the limitation
    print(f"\n  NOTE: To run a true A/B behavioral test:")
    print(f"  1. Give me a targeted bug-fix task")
    print(f"  2. Tell me to ignore memory lessons (control)")
    print(f"  3. Give the same task with lessons active (treatment)")
    print(f"  4. Compare: does the treatment response avoid scope creep?")
    print(f"  Without ANTHROPIC_API_KEY in .env, make reflect cannot auto-extract these lessons.")

    return failures


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> int:
    model_scope = os.environ.get("MEMORY_MODEL_SCOPE", "model:claude-sonnet-4-6")

    print("=" * 60)
    print(f"Memory Layer Verification — scope: {model_scope}")
    print("=" * 60)

    l1_failures = check_write_quality()
    l2_failures = check_injection_pipeline(model_scope)
    l3_failures = check_behavioral_signal(model_scope)

    total = l1_failures + l2_failures + l3_failures

    print(f"\n{'=' * 60}")
    print(f"Results: {l1_failures} Layer-1 failures | {l2_failures} Layer-2 failures | {l3_failures} Layer-3 warnings")
    if total == 0:
        print("\033[32mAll checks passed.\033[0m")
    else:
        hard = l1_failures + l2_failures
        print(f"\033[{'31' if hard else '33'}m{hard} hard failures, {l3_failures} warnings.\033[0m")
    print("=" * 60)

    return 1 if (l1_failures + l2_failures) > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
