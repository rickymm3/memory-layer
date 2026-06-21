"""Heuristic write quality scorer for memory atoms.

Returns a quality_score in [0.0, 1.0] and a list of signals explaining the score.
No LLM involved — pure rule-based Python so it runs synchronously at zero latency.

Score interpretation:
  < 0.3 → reject before entering the commit pipeline (return reason)
  0.3–0.6 → store with importance = quality_score
  > 0.6 → full pipeline at stated importance
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


# ── Ephemeral patterns: content that loses value quickly ─────────────────────

_EPHEMERAL_DATE_RE = re.compile(
    r"\b(today|tonight|yesterday|tomorrow|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"january|february|march|april|may|june|july|august|september|"
    r"october|november|december|"
    r"\d{4}-\d{2}-\d{2})\b",
    re.IGNORECASE,
)

_EPHEMERAL_NOW_RE = re.compile(
    r"\b(currently|right now|at the moment|as of now|just now|"
    r"this week|this month|this year|this sprint|this quarter)\b",
    re.IGNORECASE,
)

_TODO_RE = re.compile(
    r"\b(todo|to-do|to do|need to|needs to|should|will|going to|"
    r"plan to|planning to|next step|action item|follow up)\b",
    re.IGNORECASE,
)

_VAGUE_RE = re.compile(
    r"\b(might|maybe|perhaps|probably|possibly|i think|i believe|"
    r"not sure|unclear|unsure|depends|sort of|kind of)\b",
    re.IGNORECASE,
)

# ── Durable patterns: content with long-term value ───────────────────────────

_ARCHITECTURAL_RE = re.compile(
    r"\b(always|never|must|must not|required|constraint|rule|policy|"
    r"decision|chosen|selected|adopted|deprecated|removed|replaced|"
    r"because|therefore|reason|due to|in order to)\b",
    re.IGNORECASE,
)

# Version patterns: v1.2.3, Python 3.11, React 18, etc.
_VERSION_RE = re.compile(
    r"\bv?\d+\.\d+(?:\.\d+)?\b|python\s+\d+\.\d+|node\s+\d+",
    re.IGNORECASE,
)

# Proper nouns heuristic: CamelCase words not at sentence start
_PROPER_NOUN_RE = re.compile(r"(?<!\.\s)(?<!\A)(?<!\n)\b[A-Z][a-zA-Z]{2,}\b")

# Named technology identifiers that signal specificity
_TECH_NAME_RE = re.compile(
    r"\b(postgres|postgresql|sqlite|redis|mongodb|elasticsearch|kafka|"
    r"docker|kubernetes|k8s|aws|gcp|azure|react|vue|angular|next\.?js|"
    r"fastapi|django|flask|rails|spring|psycopg|sqlalchemy|prisma|"
    r"drizzle|tailwind|typescript|javascript|python|rust|go|golang)\b",
    re.IGNORECASE,
)


@dataclass
class QualityResult:
    quality_score: float
    decision: str          # "accept", "downgrade", "reject"
    signals: list[str] = field(default_factory=list)
    adjusted_importance: float | None = None


def score_write_quality(
    content: str,
    memory_type: str = "fact",
    stated_importance: float = 0.5,
) -> QualityResult:
    """Score a candidate memory atom for write quality.

    Args:
        content: The text being stored.
        memory_type: fact, decision, instruction, preference, opinion, etc.
        stated_importance: Importance the caller assigned (0–1).

    Returns:
        QualityResult with score, decision, explanation signals,
        and adjusted_importance (None means keep stated_importance).
    """
    text = content.strip()
    signals: list[str] = []
    score = 0.5  # base

    # ── Hard reject: too short ─────────────────────────────────────────────
    if len(text) < 15:
        return QualityResult(
            quality_score=0.0,
            decision="reject",
            signals=["content too short (< 15 chars)"],
        )

    # ── Hard reject: clearly a status update or question ──────────────────
    if text.rstrip().endswith("?"):
        return QualityResult(
            quality_score=0.0,
            decision="reject",
            signals=["content is a question, not a durable fact"],
        )

    # ── Durable type bonus ─────────────────────────────────────────────────
    if memory_type in ("decision", "instruction", "constraint"):
        score += 0.15
        signals.append(f"+0.15 durable memory_type={memory_type}")

    # ── Ephemeral penalties ────────────────────────────────────────────────
    date_matches = _EPHEMERAL_DATE_RE.findall(text)
    if date_matches:
        penalty = min(0.20, 0.05 * len(date_matches))
        score -= penalty
        signals.append(f"-{penalty:.2f} date reference: {date_matches[:2]}")

    now_matches = _EPHEMERAL_NOW_RE.findall(text)
    if now_matches:
        penalty = min(0.15, 0.08 * len(now_matches))
        score -= penalty
        signals.append(f"-{penalty:.2f} temporal reference: {now_matches[:2]}")

    todo_matches = _TODO_RE.findall(text)
    if todo_matches:
        penalty = min(0.20, 0.07 * len(todo_matches))
        score -= penalty
        signals.append(f"-{penalty:.2f} to-do / action phrasing: {todo_matches[:2]}")

    vague_matches = _VAGUE_RE.findall(text)
    if vague_matches:
        penalty = min(0.15, 0.05 * len(vague_matches))
        score -= penalty
        signals.append(f"-{penalty:.2f} vague qualifiers: {vague_matches[:2]}")

    # ── Specificity bonuses ────────────────────────────────────────────────
    arch_matches = _ARCHITECTURAL_RE.findall(text)
    if arch_matches:
        bonus = min(0.15, 0.04 * len(arch_matches))
        score += bonus
        signals.append(f"+{bonus:.2f} architectural language: {arch_matches[:3]}")

    if _VERSION_RE.search(text):
        score += 0.08
        signals.append("+0.08 version number present")

    tech_matches = _TECH_NAME_RE.findall(text)
    if tech_matches:
        bonus = min(0.12, 0.04 * len(set(m.lower() for m in tech_matches)))
        score += bonus
        signals.append(f"+{bonus:.2f} named technology: {list(set(m.lower() for m in tech_matches))[:3]}")

    proper_nouns = _PROPER_NOUN_RE.findall(text)
    if len(proper_nouns) >= 2:
        score += 0.05
        signals.append(f"+0.05 proper nouns present: {proper_nouns[:3]}")

    # ── Length bonus (longer content is usually more specific) ────────────
    if len(text) >= 80:
        score += 0.05
        signals.append("+0.05 content length >= 80 chars")

    # ── Clamp ─────────────────────────────────────────────────────────────
    score = round(max(0.0, min(1.0, score)), 3)

    # ── Decision ──────────────────────────────────────────────────────────
    if score < 0.3:
        return QualityResult(
            quality_score=score,
            decision="reject",
            signals=signals,
        )
    elif score < 0.6:
        return QualityResult(
            quality_score=score,
            decision="downgrade",
            signals=signals,
            adjusted_importance=round(min(stated_importance, score), 3),
        )
    else:
        return QualityResult(
            quality_score=score,
            decision="accept",
            signals=signals,
        )
