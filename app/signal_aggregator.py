"""Signal aggregation logic for memory_atoms.

Pure computation — no database connections. Accepts a list of signal dicts
and returns derived weight fields that get written back to memory_atoms.

Relationship classification:
  AGREEING   : new, reinforcement, refinement  → count toward support_weight
  CONFLICTING: conflict, opinion_change         → count toward opposition_weight
  Anything else is ignored.

Per-signal weight = signal.confidence × source_decay × recency_decay
  source_decay : first signal from a source = 1.0, second = 0.5, third = 0.25, …
                 (geometric, ordered chronologically)
  recency_decay: exponential half-life of 30 days (recent signals count more)

Output fields:
  support_weight    — weighted sum of agreeing signals
  opposition_weight — weighted sum of conflicting signals
  disagreement_score— opposition / (support + opposition), 0.0 if no signals
  confidence        — Bayesian-style estimate: 0.5 neutral prior, rises with
                      pure support, falls with pure conflict; clamped 0.1–0.99
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

# Relationships that support the atom's truth
AGREEING: frozenset[str] = frozenset({"new", "reinforcement", "refinement"})

# Relationships that oppose the atom's truth
CONFLICTING: frozenset[str] = frozenset({"conflict", "opinion_change"})

# Default recency half-life when no per-type override applies.
# Signal weight halves every RECENCY_HALF_LIFE_DAYS days.
RECENCY_HALF_LIFE_DAYS: float = 30.0

# Per-memory-type recency half-lives in days.
# Longer half-life → older signals retain more weight (stable, durable atoms).
# Shorter half-life → older signals lose weight faster (reactive, volatile atoms).
# Types not listed fall back to RECENCY_HALF_LIFE_DAYS.
RECENCY_HALF_LIFE_DAYS_BY_TYPE: dict[str, float] = {
    "instruction": 180.0,  # instructions are durable; very slow decay
    "decision":     90.0,  # decisions persist but can be revisited
    "fact":         90.0,  # facts are stable; moderate decay
    "opinion":      14.0,  # opinions shift; fast decay
    "preference":   14.0,  # preferences shift; fast decay
}

# Per-source spam / adversarial-source detection.
# A source whose conflict ratio meets SPAM_CONFLICT_RATIO_THRESHOLD (and has
# contributed at least SPAM_MIN_SIGNAL_COUNT signals globally) is treated as
# adversarial and its signals are multiplied by SOURCE_TRUST_FLOOR rather than 1.0.
SPAM_MIN_SIGNAL_COUNT: int = 5
SPAM_CONFLICT_RATIO_THRESHOLD: float = 0.7
SOURCE_TRUST_FLOOR: float = 0.1


def _recency_weight(created_at: "datetime | str | None", half_life_days: float = RECENCY_HALF_LIFE_DAYS) -> float:
    """Exponential decay factor: 1.0 for brand-new signals, halves every half_life_days."""
    if created_at is None:
        return 1.0
    if isinstance(created_at, str):
        try:
            created_at = datetime.fromisoformat(created_at)
        except (ValueError, TypeError):
            return 1.0
    now = datetime.now(timezone.utc)
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    age_days = max(0.0, (now - created_at).total_seconds() / 86400.0)
    return math.exp(-math.log(2) * age_days / half_life_days)


def compute_source_trust(
    source_stats: list[dict[str, Any]],
) -> dict[str, float]:
    """Compute per-source trust multipliers from global signal history.

    A source is adversarial when it has contributed at least SPAM_MIN_SIGNAL_COUNT
    signals globally AND its conflict ratio (conflicting / total) is at or above
    SPAM_CONFLICT_RATIO_THRESHOLD.  Adversarial sources receive SOURCE_TRUST_FLOOR
    as their multiplier.  All other sources are absent from the returned dict
    (callers treat a missing key as 1.0).

    Args:
        source_stats: List of dicts with keys:
            source_key (str)
            total      (int) — total signals from this source across all atoms
            conflicts  (int) — signals with relationship in CONFLICTING

    Returns:
        dict mapping adversarial source_key → SOURCE_TRUST_FLOOR.
        Non-adversarial sources are absent (default trust = 1.0).
    """
    trust: dict[str, float] = {}
    for row in source_stats:
        source_key = (row.get("source_key") or "unknown").strip()
        total = int(row.get("total") or 0)
        conflicts = int(row.get("conflicts") or 0)
        if total < SPAM_MIN_SIGNAL_COUNT:
            continue
        if (conflicts / total) >= SPAM_CONFLICT_RATIO_THRESHOLD:
            trust[source_key] = SOURCE_TRUST_FLOOR
    return trust


def compute_atom_weights(
    signals: list[dict[str, Any]],
    memory_type: str | None = None,
    source_trust: dict[str, float] | None = None,
) -> dict[str, float]:
    """Compute aggregated weights from a memory atom's signal history.

    Args:
        signals: List of dicts with keys:
            relationship   (str | None)
            confidence     (float | None)  — defaults to 0.8 if absent
            source_key     (str | None)    — defaults to "unknown"
            source_user_id (str | None)    — explicit user identity; falls back to source_key
            created_at     (datetime | None)
        memory_type: The atom's memory_type string (e.g. "instruction", "opinion").
            Used to look up the per-type recency half-life from
            RECENCY_HALF_LIFE_DAYS_BY_TYPE. Defaults to RECENCY_HALF_LIFE_DAYS
            when None or not in the lookup table.
        source_trust: Optional mapping of source_key → trust multiplier produced
            by compute_source_trust().  Missing keys default to 1.0 (full trust).
            Adversarial sources receive SOURCE_TRUST_FLOOR, which reduces their
            contribution without silencing them entirely.

    Returns:
        {
            "support_weight":      float,
            "opposition_weight":   float,
            "disagreement_score":  float,  # 0.0 when no scoreable signals
            "confidence":          float,  # clamped 0.1–0.99
            "unique_source_count": int,    # distinct user identities with scoreable signals
        }
    """
    half_life = RECENCY_HALF_LIFE_DAYS_BY_TYPE.get(
        (memory_type or "").strip().lower(), RECENCY_HALF_LIFE_DAYS
    )

    # Sort chronologically so source-decay index matches arrival order
    sorted_sigs = sorted(
        signals,
        key=lambda s: (
            s.get("created_at") or datetime.min.replace(tzinfo=timezone.utc)
        ),
    )

    source_counts: dict[str, int] = {}
    unique_identities: set[str] = set()
    support_weight = 0.0
    opposition_weight = 0.0

    for sig in sorted_sigs:
        relationship = (sig.get("relationship") or "").strip().lower()
        if relationship not in AGREEING and relationship not in CONFLICTING:
            continue

        source_key = (sig.get("source_key") or "unknown").strip()
        n = source_counts.get(source_key, 0)
        source_counts[source_key] = n + 1

        # Track unique identities: prefer explicit source_user_id; fall back to source_key.
        identity = (sig.get("source_user_id") or source_key).strip()
        unique_identities.add(identity)

        # Geometric source decay: first from this source = 1.0, second = 0.5, …
        source_decay = 0.5 ** n
        recency = _recency_weight(sig.get("created_at"), half_life)
        sig_confidence = float(sig.get("confidence") or 0.8)
        # Trust multiplier: adversarial sources are pre-penalised globally.
        trust_mult = (source_trust or {}).get(source_key, 1.0)
        weight = sig_confidence * source_decay * recency * trust_mult

        if relationship in AGREEING:
            support_weight += weight
        else:
            opposition_weight += weight

    unique_source_count = len(unique_identities)
    total = support_weight + opposition_weight
    disagreement_score = (opposition_weight / total) if total > 0.0 else 0.0

    # Bayesian-style confidence with 0.5 neutral prior.
    # Pure support → approaches ~1.0; pure conflict → approaches ~0.0.
    support_score = support_weight / (support_weight + 1.0)
    opposition_penalty = opposition_weight / (opposition_weight + 1.0)
    confidence = 0.5 + 0.5 * support_score - 0.5 * opposition_penalty
    confidence = max(0.1, min(0.99, confidence))

    # retrieval_priority: pre-computed ranking multiplier stored on the atom.
    # Multi-source corroboration boosts priority logarithmically (diminishing returns).
    # High-confidence, low-conflict, multi-source atoms rank highest.
    import math
    corroboration_bonus = 1.0 + math.log(max(1, unique_source_count)) * 0.1
    retrieval_priority = round(
        confidence * max(0.3, 1.0 - disagreement_score * 0.5) * corroboration_bonus, 3
    )
    retrieval_priority = min(retrieval_priority, 1.0)

    return {
        "support_weight": round(support_weight, 6),
        "opposition_weight": round(opposition_weight, 6),
        "disagreement_score": round(disagreement_score, 6),
        "confidence": round(confidence, 3),
        "unique_source_count": unique_source_count,
        "retrieval_priority": retrieval_priority,
    }
