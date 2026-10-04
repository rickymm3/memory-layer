from __future__ import annotations

from typing import Any

from app.db import get_store
from app.retrieval_policy import CONFLICT_THRESHOLD, format_conflict_lines, is_conflicted
from mcp_server.auth_context import current_user_id

USER_SCOPE = "user"


def get_task_context(
    project_scope: str,
    model_scope: str | None = None,
    task_hint: str | None = None,
    recent_tasks: int = 5,
    compact: bool = True,
) -> dict[str, Any]:
    """Return a complete task-orientation snapshot for a project+model pair.

    Combines in one call:
    1. project_context  — high-importance, high-confidence atoms for the project scope.
    2. user_context     — the user's high-importance cross-project preferences.
    3. model_lessons    — all active atoms from model_scope (weaknesses, adaptations).
    4. recent_task_runs — last N task_run records for this project.
    5. task_relevant_atoms — semantic matches for task_hint across the project
       and user scopes (if provided, ~500 ms).

    Every section applies the caller's visibility access rule. Contested atoms
    come back with both sides: each claim and who made it.

    Args:
        project_scope: Required. E.g. 'project:memory-layer'.
        model_scope: Optional model scope (e.g. 'model:claude-sonnet-4-6').
        task_hint: Optional short description for semantic search.
        recent_tasks: Number of recent task_runs to return. Clamped 1–20. Default 5.
        compact: When True (default), return atoms as compact strings
            '[type] (conf) content' instead of full JSON dicts. Cuts token
            cost by ~80% for session-start injection.
    """
    clamped_tasks = max(1, min(int(recent_tasks), 20))
    store = get_store()
    user = current_user_id.get()

    # 1. project_context — reduced from 15 to 8; session-start must be lean
    project_atoms = store.project_context_atoms(
        scope=project_scope, limit=8, min_importance=0.6, min_confidence=0.65,
        requesting_user=user,
    )

    # 2. user_context — cross-project preferences travel into every project
    user_atoms: list[dict[str, Any]] = []
    if project_scope != USER_SCOPE:
        user_atoms = store.project_context_atoms(
            scope=USER_SCOPE, limit=5, min_importance=0.6, min_confidence=0.65,
            requesting_user=user,
        )

    # 3. model_lessons — reduced from 20 to 5; model-specific atoms rarely numerous
    model_lessons = (
        store.get_active_atoms_by_scope(scope=model_scope, limit=5, requesting_user=user)
        if model_scope else []
    )

    # 4. recent_task_runs
    task_runs = store.list_task_runs_db(scope=project_scope, limit=clamped_tasks)

    # 5. task_relevant_atoms (semantic search over project + user scopes)
    task_relevant: list[Any] = []
    if task_hint:
        task_relevant = store.retrieve_memories(
            query=task_hint,
            limit=5,
            min_similarity=0.45,
            scope_filters=list(dict.fromkeys([project_scope, USER_SCOPE])),
            requesting_user=user,
        )
        for a in task_relevant:
            if "disagreement_flag" not in a:
                a["disagreement_flag"] = a.get("disagreement_score", 0.0) > CONFLICT_THRESHOLD

    # project/user/model sections are not similarity-ranked, so attach
    # conflict detail to them here.
    store.attach_conflicts(project_atoms + user_atoms + model_lessons)

    outcome_counts: dict[str, int] = {}
    for tr in task_runs:
        outcome_counts[tr["outcome"]] = outcome_counts.get(tr["outcome"], 0) + 1

    def _fmt(atoms: list[dict[str, Any]], prefer_injection: bool = False) -> list[Any]:
        if not compact:
            return atoms
        result = []
        for a in atoms:
            # For model lessons, inject the actionable context_summary (the directive
            # that gets prepended to prompts), not the full descriptive content.
            if prefer_injection and a.get("context_summary"):
                text = a["context_summary"]
            else:
                text = a["content"]
            if is_conflicted(a) and a.get("conflict"):
                # Report the disagreement as attributed claims, not a hedged belief.
                lines = format_conflict_lines(a)
                result.append(
                    f"[{a['memory_type']}] CONTESTED — not settled; report each claim "
                    f"with who made it: {text}" + ("\n" + "\n".join(lines) if lines else "")
                )
                continue
            flag = " ⚠ conflict" if a.get("disagreement_flag") else ""
            result.append(f"[{a['memory_type']}] ({float(a.get('confidence', 0.5)):.2f}) {text}{flag}")
        return result

    def _fmt_runs(runs: list[dict[str, Any]]) -> list[Any]:
        if not compact:
            return runs
        return [f"[{r['outcome']}] {r.get('task_description', '')[:80]}" for r in runs]

    return {
        "project_scope": project_scope,
        "model_scope": model_scope,
        "compact": compact,
        "project_context": _fmt(project_atoms),
        "user_context": _fmt(user_atoms),
        "model_lessons": _fmt(model_lessons, prefer_injection=True),
        "recent_task_runs": _fmt_runs(task_runs),
        "task_relevant_atoms": _fmt(task_relevant),
        "summary": {
            "project_atoms": len(project_atoms),
            "user_atoms": len(user_atoms),
            "model_lessons": len(model_lessons),
            "recent_task_runs": len(task_runs),
            "task_relevant_atoms": len(task_relevant),
            "task_run_outcomes": outcome_counts,
        },
        "write_protocol": (
            "Before ending a turn in which the user made a decision, stated a "
            "constraint or preference, or corrected you, call memory_store_auto. "
            f"Scope is required: project facts → scope='{project_scope}', "
            "the user's cross-project preferences → scope='user', "
            "observations about the model → scope='model:<id>'. "
            "Visibility defaults to private. Write one self-contained sentence that "
            "would still be true in another chat; skip questions, task restatements, "
            "and pleasantries. Report memory_atom_id and memory_signal_id."
        ),
    }
