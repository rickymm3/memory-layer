"""Confidence-gated task readiness assessment.

Evaluates whether the agent has sufficient context to proceed with a task,
based on the output of memory_task_context (or equivalent data). Returns
a structured verdict with recommended action, reasons, risks, and gaps.

Design principles
-----------------
- Deterministic and inspectable: all rules are explicit code, not LLM inference.
- No side effects: pure function, no DB queries, no network calls.
- Works with memory_task_context output as-is (requires disagreement_flag field).
- Conservative by default: bias toward "get more context" over "blindly proceed".
"""
from __future__ import annotations

import re
from typing import Any

# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

# Keywords suggesting current / versioned / external information is needed.
_CURRENT_INFO_RE = re.compile(
    r"\b(current|latest|newest|today|recent|up-to-date|up to date"
    r"|best practice|api|library|version|changelog|release"
    r"|documentation|docs|what is|what are)\b",
    re.IGNORECASE,
)

# Keywords indicating a coding / mutation task (test spec should be required).
_CODING_TASK_RE = re.compile(
    r"\b(implement|add|create|write|build|fix|refactor|migrate"
    r"|change|update|modify|edit|delete|remove|extend|develop)\b",
    re.IGNORECASE,
)

# Keywords indicating test / verification requirements are present in the task.
_TEST_SPEC_RE = re.compile(
    r"\b(test|tests|spec|specs|assert|verify|check|validate"
    r"|make doctor|pytest|unittest|assertion)\b",
    re.IGNORECASE,
)

# Model-lesson content that signals the model needs explicit test guardrails.
_MODEL_NEEDS_TESTS_RE = re.compile(
    r"\b(explicit test|test requirement|skip verification|run tests"
    r"|test before|require test|needs test)\b",
    re.IGNORECASE,
)

# Default threshold below which we don't recommend "proceed".
DEFAULT_CONFIDENCE_THRESHOLD = 0.60


def assess_task_readiness(
    task_description: str,
    project_scope: str,
    model_scope: str | None,
    task_context: dict[str, Any],
    user_requested_web: bool = False,
    task_hint: str | None = None,
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
) -> dict[str, Any]:
    """Evaluate whether the agent has sufficient context to proceed.

    Parameters
    ----------
    task_description:
        Short description of the task to be performed.
    project_scope:
        Project scope string (e.g. 'project:memory-layer').
    model_scope:
        Model scope string (e.g. 'model:qwen3-8b'), or None.
    task_context:
        Output from memory_task_context (or equivalent). Expected keys:
        - project_context: list[dict] — project atoms
        - model_lessons: list[dict] — model-scope atoms
        - recent_task_runs: list[dict] — recent task run records
        - task_relevant_atoms: list[dict] — semantically matched atoms
        Each atom dict should include 'disagreement_flag' and 'lifecycle_status'
        (both present after task_context.py was updated for Phase 7.7).
    user_requested_web:
        True if the user's request explicitly asks for web/current information.
    task_hint:
        Optional hint string passed to memory_task_context. Used to distinguish
        "hint given but nothing matched" from "no hint given" when
        task_relevant_atoms is empty.
    confidence_threshold:
        Confidence below which ready=False regardless of other rules.
        Default 0.60.

    Returns
    -------
    dict with keys:
        ready: bool
        confidence: float (0.0–1.0)
        recommended_action: str — one of:
            "proceed" | "retrieve_more" | "search_web" | "ask_user" |
            "inspect_conflict" | "define_tests" | "project_kickoff"
        reasons: list[str]
        risks: list[str]
        missing: list[str]
        suggested_search_query: str | None
        required_checks: list[str]
    """
    project_context: list[dict] = task_context.get("project_context", [])
    model_lessons: list[dict] = task_context.get("model_lessons", [])
    task_relevant_atoms: list[dict] = task_context.get("task_relevant_atoms", [])

    reasons: list[str] = []
    risks: list[str] = []
    missing: list[str] = []
    required_checks: list[str] = []
    confidence_penalties: list[float] = []
    # (priority_ascending = more_urgent, action_string)
    action_candidates: list[tuple[int, str]] = []

    is_coding = bool(_CODING_TASK_RE.search(task_description))
    needs_current = user_requested_web or bool(_CURRENT_INFO_RE.search(task_description))
    has_test_spec = bool(_TEST_SPEC_RE.search(task_description))

    # ── Rule 1: empty project context ─────────────────────────────────────
    if not project_context:
        reasons.append(
            f"No project context found for scope '{project_scope}'. "
            "The agent has no stored constraints, decisions, or guardrails."
        )
        missing.append("project context")
        confidence_penalties.append(0.5)
        action_candidates.append((10, "project_kickoff"))

    # ── Rule 2: task hint provided but no relevant atoms matched ──────────
    if task_hint and not task_relevant_atoms:
        reasons.append(
            "A task hint was provided but no semantically relevant atoms were found "
            "in the project scope. Context may be insufficient for this specific task."
        )
        missing.append("task-relevant project atoms")
        confidence_penalties.append(0.2)
        action_candidates.append((60, "retrieve_more"))
    elif project_context and len(project_context) < 3 and not task_hint:
        # Thin context even without a task hint
        risks.append(
            f"Only {len(project_context)} project atom(s) available — context may be sparse."
        )
        confidence_penalties.append(0.1)
        action_candidates.append((65, "retrieve_more"))

    # ── Rule 3: contested or non-active relevant atoms ────────────────────
    all_relevant = list(project_context) + list(task_relevant_atoms)
    contested = [
        a for a in all_relevant
        if a.get("disagreement_flag") is True
        or a.get("lifecycle_status", "active") != "active"
    ]
    if contested:
        summaries = [
            (a.get("context_summary") or a.get("content", ""))[:80]
            for a in contested[:3]
        ]
        risks.append(
            f"{len(contested)} relevant atom(s) are contested or non-active: "
            + "; ".join(f'"{s}"' for s in summaries)
        )
        confidence_penalties.append(0.25)
        action_candidates.append((20, "inspect_conflict"))
        required_checks.append(
            "Review contested/non-active atoms before proceeding: "
            + ", ".join(a.get("id", "?")[:8] for a in contested[:3])
        )

    # ── Rule 4: needs current / external information ──────────────────────
    if needs_current:
        reasons.append(
            "Task description contains keywords suggesting current, versioned, or "
            "external information may be needed (e.g. 'latest', 'API', 'current')."
        )
        risks.append("Stored memory may be outdated for this type of query.")
        confidence_penalties.append(0.15)
        action_candidates.append((30, "search_web"))

    # ── Rule 5: coding task without test specification ────────────────────
    if is_coding and not has_test_spec:
        reasons.append(
            "This appears to be a coding task but no test or verification "
            "requirements were specified in the task description."
        )
        missing.append("test/verification requirements")
        confidence_penalties.append(0.15)
        action_candidates.append((40, "define_tests"))
        required_checks.append("Define test/verification requirements before coding begins.")

    # ── Rule 6: model lessons require explicit test / guardrails ──────────
    model_test_warnings: list[str] = []
    for lesson in model_lessons:
        text = (lesson.get("content", "") + " " + lesson.get("context_summary", "")).strip()
        if _MODEL_NEEDS_TESTS_RE.search(text):
            summary = (lesson.get("context_summary") or lesson.get("content", ""))[:80]
            model_test_warnings.append(summary)
    if model_test_warnings:
        for w in model_test_warnings:
            required_checks.append(f"Model lesson: {w}")
        if not has_test_spec:
            reasons.append(
                f"Model lessons ({len(model_test_warnings)} lesson(s)) indicate this model "
                "needs explicit test requirements before proceeding."
            )
            confidence_penalties.append(0.1)
            if (40, "define_tests") not in action_candidates:
                action_candidates.append((40, "define_tests"))

    # ── Rule 7: compute confidence, verdict, recommended action ───────────
    total_penalty = min(sum(confidence_penalties), 1.0)
    confidence = max(0.0, round(1.0 - total_penalty, 3))

    # Hard blockers that always set ready=False
    hard_blockers = {"project_kickoff", "inspect_conflict"}
    has_hard_blocker = any(a in hard_blockers for _, a in action_candidates)
    ready = confidence >= confidence_threshold and not has_hard_blocker

    if action_candidates:
        action_candidates.sort(key=lambda x: x[0])
        recommended_action = action_candidates[0][1]
    else:
        recommended_action = "proceed"

    # If confidence is too low but no specific blocker, fall back to retrieve_more
    if not ready and recommended_action == "proceed":
        recommended_action = "retrieve_more"

    # Suggested search query for search/retrieve cases
    suggested_query: str | None = None
    if recommended_action in ("search_web", "retrieve_more") or needs_current:
        slug = project_scope.replace("project:", "").replace(":", " ")
        suggested_query = f"{task_description} {slug}".strip()

    if not reasons and not risks:
        reasons.append("Sufficient project context available. Ready to proceed.")

    return {
        "ready": ready,
        "confidence": confidence,
        "recommended_action": recommended_action,
        "reasons": reasons,
        "risks": risks,
        "missing": missing,
        "suggested_search_query": suggested_query,
        "required_checks": required_checks,
    }
