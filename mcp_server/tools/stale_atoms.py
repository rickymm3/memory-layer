from __future__ import annotations

from typing import Any

from app.db import get_store


def get_stale_atoms(
    days_threshold: int = 90,
    min_disagreement: float = 0.4,
    scope: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Surface atoms that may need review: contested, old+unsupported, or volatile+aged.

    Three staleness signals are checked independently:
    - **Contested**: disagreement_score >= min_disagreement (conflicting signals on record).
    - **Old + unsupported**: created more than days_threshold days ago with support_weight < 0.5
      (no one has reinforced the atom since it was created).
    - **Volatile + aged**: memory_type is opinion/preference/lesson/belief and created > 30 days
      ago (these types decay naturally and should be revalidated periodically).

    Each returned atom includes a `staleness_reasons` list explaining why it was flagged.

    Args:
        days_threshold: Age in days beyond which low-support atoms are considered stale. Default 90.
        min_disagreement: disagreement_score threshold for contested atoms. Default 0.4.
        scope: Restrict to a specific scope. If omitted, checks all scopes.
        limit: Maximum atoms to return. Clamped to 1–100. Default 20.

    Returns:
        List of atoms with staleness_reasons, age_days, disagreement_score, and support_weight.
    """
    clamped_limit = max(1, min(int(limit), 100))
    clamped_days = max(1, int(days_threshold))
    clamped_disagreement = max(0.0, min(1.0, float(min_disagreement)))

    return get_store().get_stale_atoms(
        days_threshold=clamped_days,
        min_disagreement=clamped_disagreement,
        scope=scope,
        limit=clamped_limit,
    )
