from __future__ import annotations

from typing import Any

from app.db import get_store


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
    2. model_lessons    — all active atoms from model_scope (weaknesses, adaptations).
    3. recent_task_runs — last N task_run records for this project.
    4. task_relevant_atoms — semantic matches for task_hint (if provided, ~500 ms).

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

    # 1. project_context — reduced from 15 to 8; session-start must be lean
    project_atoms = store.project_context_atoms(
        scope=project_scope, limit=8, min_importance=0.6, min_confidence=0.65
    )

    # 2. model_lessons — reduced from 20 to 5; model-specific atoms rarely numerous
    model_lessons = store.get_active_atoms_by_scope(scope=model_scope, limit=5) if model_scope else []

    # 3. recent_task_runs
    task_runs = store.list_task_runs_db(scope=project_scope, limit=clamped_tasks)

    # 4. task_relevant_atoms (semantic search)
    task_relevant: list[Any] = []
    if task_hint is not None:
        task_relevant = store.retrieve_memories(
            query=task_hint,
            limit=5,
            min_similarity=0.45,
            scope_filter=project_scope,
        )
        for a in task_relevant:
            if "disagreement_flag" not in a:
                a["disagreement_flag"] = a.get("disagreement_score", 0.0) >= 0.5

    outcome_counts: dict[str, int] = {}
    for tr in task_runs:
        outcome_counts[tr["outcome"]] = outcome_counts.get(tr["outcome"], 0) + 1

    def _fmt(atoms: list[dict[str, Any]]) -> list[Any]:
        if not compact:
            return atoms
        result = []
        for a in atoms:
            flag = " ⚠ conflict" if a.get("disagreement_flag") else ""
            result.append(f"[{a['memory_type']}] ({float(a.get('confidence', 0.5)):.2f}) {a['content']}{flag}")
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
        "model_lessons": _fmt(model_lessons),
        "recent_task_runs": _fmt_runs(task_runs),
        "task_relevant_atoms": _fmt(task_relevant),
        "summary": {
            "project_atoms": len(project_atoms),
            "model_lessons": len(model_lessons),
            "recent_task_runs": len(task_runs),
            "task_relevant_atoms": len(task_relevant),
            "task_run_outcomes": outcome_counts,
        },
    }
