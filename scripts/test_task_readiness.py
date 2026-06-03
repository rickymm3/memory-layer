#!/usr/bin/env python
"""Tests for app/task_readiness.py — covers all 7 rules.

Run: python scripts/test_task_readiness.py
or:  make test-task-readiness
"""
from __future__ import annotations

import sys

sys.path.insert(0, ".")

from app.task_readiness import assess_task_readiness

PASS = "PASS"
FAIL = "FAIL"
results: list[tuple[str, str]] = []


def check(name: str, condition: bool) -> None:
    status = PASS if condition else FAIL
    results.append((name, status))
    marker = "+" if condition else "x"
    print(f"  [{status}] {marker} {name}")


def section(title: str) -> None:
    print(f"\n-- {title}")


def _atoms(n: int) -> list[dict]:
    return [
        {"id": str(i), "content": f"fact {i}", "lifecycle_status": "active"}
        for i in range(n)
    ]


# ── Test 1: empty project context blocks and proposes kickoff ─────────────
section("Test 1: empty project context → project_kickoff, not ready")
r = assess_task_readiness(
    "implement auth module",
    "project:foo",
    None,
    {"project_context": [], "model_lessons": [], "task_relevant_atoms": []},
)
check("not ready", r["ready"] is False)
check("recommended_action == project_kickoff", r["recommended_action"] == "project_kickoff")
check("'project context' in missing", "project context" in r["missing"])
check("confidence < 0.6", r["confidence"] < 0.6)

# ── Test 2: current/latest/API task recommends search_web ─────────────────
section("Test 2: current/latest/API task → search_web")
r = assess_task_readiness(
    "what is the latest version of FastAPI",
    "project:foo",
    None,
    {"project_context": _atoms(6), "model_lessons": [], "task_relevant_atoms": []},
)
check("recommended_action == search_web", r["recommended_action"] == "search_web")
check("suggested_search_query is not None", r["suggested_search_query"] is not None)

# ── Test 3: coding task without tests → define_tests ──────────────────────
section("Test 3: coding task without test spec → define_tests")
r = assess_task_readiness(
    "implement OAuth login handler",
    "project:foo",
    None,
    {"project_context": _atoms(6), "model_lessons": [], "task_relevant_atoms": []},
)
check("recommended_action == define_tests", r["recommended_action"] == "define_tests")
check("'test/verification requirements' in missing", "test/verification requirements" in r["missing"])
check("required_checks not empty", len(r["required_checks"]) > 0)

# ── Test 4: contested atom → inspect_conflict ──────────────────────────────
section("Test 4: contested atom → inspect_conflict, not ready")
r = assess_task_readiness(
    "update auth module",
    "project:foo",
    None,
    {
        "project_context": [
            {
                "id": "abc123ef",
                "content": "auth decision",
                "lifecycle_status": "active",
                "disagreement_flag": True,
                "context_summary": "auth decision atom",
            },
            {"id": "def456", "content": "safe fact", "lifecycle_status": "active"},
        ],
        "model_lessons": [],
        "task_relevant_atoms": [],
    },
)
check("recommended_action == inspect_conflict", r["recommended_action"] == "inspect_conflict")
check("not ready (hard blocker)", r["ready"] is False)
check("risks not empty", len(r["risks"]) > 0)
check("required_checks mention atom id", any("abc123ef" in c for c in r["required_checks"]))

# ── Test 5: sufficient context → proceed ──────────────────────────────────
section("Test 5: sufficient context, non-coding task → proceed")
r = assess_task_readiness(
    "run the existing test suite and report results",
    "project:foo",
    None,
    {"project_context": _atoms(8), "model_lessons": [], "task_relevant_atoms": []},
)
check("recommended_action == proceed", r["recommended_action"] == "proceed")
check("ready == True", r["ready"] is True)
check("confidence >= 0.6", r["confidence"] >= 0.6)

# ── Test 6: model lesson requiring tests → required_checks ────────────────
section("Test 6: model lesson with test requirement → required_checks")
r = assess_task_readiness(
    "run the existing test suite and report results",
    "project:foo",
    "model:qwen3-8b",
    {
        "project_context": _atoms(8),
        "model_lessons": [
            {
                "id": "m1",
                "content": "qwen3:8b needs explicit test requirements before coding tasks",
                "context_summary": "explicit test requirements are necessary",
                "lifecycle_status": "active",
            }
        ],
        "task_relevant_atoms": [],
    },
)
check("required_checks not empty", len(r["required_checks"]) > 0)
check("required_checks contain 'Model lesson'", any("Model lesson" in c for c in r["required_checks"]))

# ── Test 7: task_hint given, no relevant atoms → retrieve_more ─────────────
section("Test 7: task_hint given, no relevant atoms → retrieve_more")
r = assess_task_readiness(
    "run existing tests",
    "project:foo",
    None,
    {"project_context": _atoms(6), "model_lessons": [], "task_relevant_atoms": []},
    task_hint="confidence gated agent loop implementation",
)
check("recommended_action == retrieve_more", r["recommended_action"] == "retrieve_more")
check("'task-relevant project atoms' in missing", "task-relevant project atoms" in r["missing"])

# ── Test 8: non-coding task with no test spec → no define_tests ───────────
section("Test 8: read-only query task does not trigger define_tests")
r = assess_task_readiness(
    "list all stored memory atoms in scope project:foo",
    "project:foo",
    None,
    {"project_context": _atoms(6), "model_lessons": [], "task_relevant_atoms": []},
)
check("recommended_action != define_tests", r["recommended_action"] != "define_tests")

# ── Test 9: user_requested_web=True triggers search_web ───────────────────
section("Test 9: user_requested_web=True → search_web")
r = assess_task_readiness(
    "summarise the project structure",
    "project:foo",
    None,
    {"project_context": _atoms(8), "model_lessons": [], "task_relevant_atoms": []},
    user_requested_web=True,
)
check("recommended_action == search_web", r["recommended_action"] == "search_web")

# ── Test 10: lifecycle_status != active → inspect_conflict ────────────────
section("Test 10: deprecated atom in project_context → inspect_conflict")
r = assess_task_readiness(
    "update database schema",
    "project:foo",
    None,
    {
        "project_context": [
            {
                "id": "xyz789ab",
                "content": "old schema",
                "lifecycle_status": "deprecated",
                "context_summary": "deprecated schema atom",
            }
        ]
        + _atoms(5),
        "model_lessons": [],
        "task_relevant_atoms": [],
    },
)
check("recommended_action == inspect_conflict", r["recommended_action"] == "inspect_conflict")
check("not ready", r["ready"] is False)

# ── Summary ───────────────────────────────────────────────────────────────
print()
passed = sum(1 for _, s in results if s == PASS)
total = len(results)
ok = passed == total
status = "OK" if ok else "FAIL"
print(f"[{status}] {passed}/{total} passed")
sys.exit(0 if ok else 1)
