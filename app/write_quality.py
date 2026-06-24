"""Epistemic write classifier for memory atoms.

Philosophy: every signal has value. Wrong facts reveal user beliefs. Vague content
reveals uncertainty. Ephemeral content reveals temporal context. Questions reveal
inquiry areas. Nothing is rejected for quality reasons — only for safety.

The classifier's job is to assign the right memory_type and confidence so that
downstream storage reflects the epistemic status of the content.

Hard-blocked (GUARDRAILS only):
  - Secrets and credentials (API keys, passwords, tokens)
  - Literally empty content
  - Invalid scope format (routing integrity)

These three are non-negotiable safety blocks. Everything else is accepted and
stored with an appropriate type and confidence level.

Epistemic tiers:
  VERIFIED  (score ≥ 0.7) — architectural decisions, policy, named tech, version pins
  REPORTED  (0.5–0.7)     — general facts, stated preferences, attributed claims
  BELIEVED  (0.3–0.5)     — vague, uncertain, or ephemeral content stored as belief
  OBSERVED  (any score)   — model:* scope always routes here at full importance
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# ── Scope validation ──────────────────────────────────────────────────────────

_VALID_SCOPE_RE = re.compile(r"^(user|project:.+|model:.+)$")


# ── GUARDRAILS — configurable hard-block list ─────────────────────────────────
#
# These patterns trigger an immediate hard reject regardless of any other signal.
# Add patterns here to extend the safety surface. Each entry is a regex that,
# if matched anywhere in the content, blocks the write.
#
# To add project-specific guardrails, extend this list in your config or at
# import time:
#   from app.write_quality import GUARDRAILS
#   GUARDRAILS.append(re.compile(r"your-pattern", re.IGNORECASE))

GUARDRAILS: list[re.Pattern] = [
    # API keys and tokens
    re.compile(r"\b(sk-ant-|sk-[a-zA-Z0-9]{20,}|Bearer\s+[a-zA-Z0-9\-._~+/]+=*)\b"),
    # Password field patterns
    re.compile(r"\b(password|passwd|secret)\s*[=:]\s*\S{4,}", re.IGNORECASE),
    # Private key blocks
    re.compile(r"-----BEGIN\s+(RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    # Database connection strings with credentials
    re.compile(
        r"(postgres|mysql|mongodb|redis)://[^:]+:[^@]+@",
        re.IGNORECASE,
    ),
]

_GUARDRAIL_LABELS = [
    "API key or token",
    "password/secret field",
    "private key block",
    "database credentials in URL",
]


# ── Epistemic classifiers ─────────────────────────────────────────────────────

# Strong certainty: architectural decisions, technical constraints, policies
_ARCHITECTURAL_RE = re.compile(
    r"\b(always|never|must|must not|required|constraint|rule|policy|"
    r"decision|chosen|selected|adopted|deprecated|removed|replaced|"
    r"because|therefore|reason|due to|in order to)\b",
    re.IGNORECASE,
)

# User belief indicators: first-person claims, uncertainty markers
_BELIEF_RE = re.compile(
    r"\b(i think|i believe|i feel|i assume|i thought|i was told|"
    r"might|maybe|perhaps|probably|possibly|not sure|unclear|"
    r"unsure|depends|sort of|kind of|seems like|it appears)\b",
    re.IGNORECASE,
)

# Temporal / ephemeral signals (lower durability, not rejection)
_EPHEMERAL_DATE_RE = re.compile(
    r"\b(today|tonight|yesterday|tomorrow|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"\d{4}-\d{2}-\d{2})\b",
    re.IGNORECASE,
)

_EPHEMERAL_NOW_RE = re.compile(
    r"\b(currently|right now|at the moment|as of now|just now|"
    r"this week|this month|this year|this sprint|this quarter)\b",
    re.IGNORECASE,
)

# Intent / action signals (store as preference/intent, not fact)
_INTENT_RE = re.compile(
    r"\b(todo|to-do|need to|needs to|going to|plan to|planning to|"
    r"next step|action item|follow up|want to|would like to)\b",
    re.IGNORECASE,
)

# Specificity boosters (push toward VERIFIED tier)
_VERSION_RE = re.compile(
    r"\bv?\d+\.\d+(?:\.\d+)?\b|python\s+\d+\.\d+|node\s+\d+",
    re.IGNORECASE,
)

_TECH_NAME_RE = re.compile(
    r"\b(postgres|postgresql|sqlite|redis|mongodb|elasticsearch|kafka|"
    r"docker|kubernetes|k8s|aws|gcp|azure|react|vue|angular|next\.?js|"
    r"fastapi|django|flask|rails|spring|psycopg|sqlalchemy|prisma|"
    r"drizzle|tailwind|typescript|javascript|python|rust|go|golang)\b",
    re.IGNORECASE,
)

_PROPER_NOUN_RE = re.compile(r"(?<!\.\s)(?<!\A)(?<!\n)\b[A-Z][a-zA-Z]{2,}\b")


# ── Result dataclass ──────────────────────────────────────────────────────────

@dataclass
class QualityResult:
    quality_score: float
    decision: str                          # "accept", "downgrade", "reject"
    signals: list[str] = field(default_factory=list)
    adjusted_importance: float | None = None
    suggested_memory_type: str | None = None   # hint to critic for reframing
    suggested_confidence: float | None = None  # suggested confidence override
    reframe_hint: str | None = None            # plain-English hint for the critic


# ── Main classifier ───────────────────────────────────────────────────────────

def score_write_quality(
    content: str,
    memory_type: str = "fact",
    stated_importance: float = 0.5,
    scope: str | None = None,
) -> QualityResult:
    """Classify a candidate memory atom for epistemic tier and storage routing.

    Returns a QualityResult with:
    - quality_score: 0–1 (how durable/verifiable the content appears)
    - decision: accept (≥0.6), downgrade (0.3–0.6), or reject (safety blocks only)
    - suggested_memory_type: hint to the critic about how to classify this content
    - suggested_confidence: recommended confidence if different from default
    - reframe_hint: guidance for the critic on how to reframe the content

    Hard-blocks (decision=reject):
    - Secrets/credentials matching GUARDRAILS patterns
    - Empty content
    - Invalid scope format

    Everything else is accepted and stored with appropriate type and confidence.
    """
    # ── Hard reject: invalid scope ─────────────────────────────────────────
    if scope is not None and not _VALID_SCOPE_RE.match(scope):
        return QualityResult(
            quality_score=0.0,
            decision="reject",
            signals=[f"invalid scope '{scope}' — must be user, project:<name>, or model:<name>"],
        )

    text = content.strip()

    # ── Hard reject: empty content ─────────────────────────────────────────
    if len(text) < 5:
        return QualityResult(
            quality_score=0.0,
            decision="reject",
            signals=["empty or near-empty content"],
        )

    # ── Hard reject: GUARDRAILS (credentials/secrets) ──────────────────────
    for pattern, label in zip(GUARDRAILS, _GUARDRAIL_LABELS):
        if pattern.search(text):
            return QualityResult(
                quality_score=0.0,
                decision="reject",
                signals=[f"guardrail block: {label} detected — secrets must not be stored"],
            )

    # ── Everything below this line is stored — only tier and framing vary ──

    signals: list[str] = []
    score = 0.50  # base

    # ── Model scope: always full durability ───────────────────────────────
    if scope and scope.startswith("model:"):
        score += 0.20
        signals.append("+0.20 model-scope (behavioral observation, always durable)")
        return QualityResult(
            quality_score=min(1.0, score + _specificity_bonus(text, signals)),
            decision="accept",
            signals=signals,
            suggested_memory_type=memory_type or "observation",
        )

    # ── Durable type declared by caller ──────────────────────────────────
    if memory_type in ("decision", "instruction", "constraint", "observation"):
        score += 0.15
        signals.append(f"+0.15 durable memory_type={memory_type}")

    # ── Epistemic tier detection ──────────────────────────────────────────

    # Belief indicators → suggest storing as belief with attributed framing
    belief_matches = _BELIEF_RE.findall(text)
    is_belief = bool(belief_matches)
    if is_belief:
        penalty = min(0.20, 0.05 * len(belief_matches))
        score -= penalty
        signals.append(f"-{penalty:.2f} belief/uncertainty markers: {belief_matches[:2]}")

    # Intent/action → store as preference or intent, not fact
    intent_matches = _INTENT_RE.findall(text)
    is_intent = bool(intent_matches)
    if is_intent:
        penalty = min(0.15, 0.05 * len(intent_matches))
        score -= penalty
        signals.append(f"-{penalty:.2f} intent/action phrasing: {intent_matches[:2]}")

    # Temporal signals → lower durability, not rejection
    date_matches = _EPHEMERAL_DATE_RE.findall(text)
    if date_matches:
        penalty = min(0.20, 0.07 * len(date_matches))
        score -= penalty
        signals.append(f"-{penalty:.2f} temporal reference (decays): {date_matches[:2]}")

    now_matches = _EPHEMERAL_NOW_RE.findall(text)
    if now_matches:
        penalty = min(0.12, 0.06 * len(now_matches))
        score -= penalty
        signals.append(f"-{penalty:.2f} now-reference (decays): {now_matches[:2]}")

    # Specificity bonuses
    score += _specificity_bonus(text, signals)

    # ── Clamp ─────────────────────────────────────────────────────────────
    score = round(max(0.05, min(1.0, score)), 3)

    # ── Suggest memory_type and reframe_hint based on detected signals ─────
    sugg_type, sugg_conf, reframe = _suggest_epistemic_framing(
        text=text,
        caller_type=memory_type,
        is_belief=is_belief,
        is_intent=is_intent,
        has_date=bool(date_matches or now_matches),
        score=score,
    )

    # ── Decision: downgrade (not reject) for low scores ───────────────────
    if score < 0.3:
        # Still stored — just at lower importance and as belief type
        return QualityResult(
            quality_score=score,
            decision="downgrade",
            signals=signals,
            adjusted_importance=round(min(stated_importance, max(0.2, score)), 3),
            suggested_memory_type=sugg_type or "belief",
            suggested_confidence=sugg_conf or 0.35,
            reframe_hint=reframe or "Store as low-confidence attributed belief.",
        )
    elif score < 0.6:
        return QualityResult(
            quality_score=score,
            decision="downgrade",
            signals=signals,
            adjusted_importance=round(min(stated_importance, score), 3),
            suggested_memory_type=sugg_type,
            suggested_confidence=sugg_conf,
            reframe_hint=reframe,
        )
    else:
        return QualityResult(
            quality_score=score,
            decision="accept",
            signals=signals,
            suggested_memory_type=sugg_type,
            suggested_confidence=sugg_conf,
            reframe_hint=reframe,
        )


def _specificity_bonus(text: str, signals: list[str]) -> float:
    """Return total specificity bonus for architectural/tech/length signals."""
    bonus = 0.0
    arch_matches = _ARCHITECTURAL_RE.findall(text)
    if arch_matches:
        b = min(0.15, 0.04 * len(arch_matches))
        bonus += b
        signals.append(f"+{b:.2f} architectural language: {arch_matches[:3]}")
    if _VERSION_RE.search(text):
        bonus += 0.08
        signals.append("+0.08 version number")
    tech_matches = _TECH_NAME_RE.findall(text)
    if tech_matches:
        b = min(0.12, 0.04 * len(set(m.lower() for m in tech_matches)))
        bonus += b
        signals.append(f"+{b:.2f} named technology: {list(set(m.lower() for m in tech_matches))[:3]}")
    proper_nouns = _PROPER_NOUN_RE.findall(text)
    if len(proper_nouns) >= 2:
        bonus += 0.05
        signals.append(f"+0.05 proper nouns: {proper_nouns[:3]}")
    if len(text) >= 80:
        bonus += 0.05
        signals.append("+0.05 length >= 80 chars")
    return bonus


def _suggest_epistemic_framing(
    text: str,
    caller_type: str,
    is_belief: bool,
    is_intent: bool,
    has_date: bool,
    score: float,
) -> tuple[str | None, float | None, str | None]:
    """Return (suggested_memory_type, suggested_confidence, reframe_hint)."""

    # Caller explicitly set a durable type → respect it
    if caller_type in ("decision", "instruction", "constraint", "observation"):
        return caller_type, None, None

    if is_belief:
        return (
            "belief",
            0.35,
            "Rewrite as an attributed user claim: 'The user believes/asserts X.' "
            "Do not present as verified fact. Store at low confidence.",
        )

    if is_intent:
        return (
            "preference",
            0.45,
            "Rewrite as a user intent or preference: 'The user intends/wants X.' "
            "Store as preference type.",
        )

    if has_date and score < 0.5:
        return (
            "observation",
            0.45,
            "Temporal reference detected. Store as time-bound observation. "
            "Include the temporal context in the rewritten content.",
        )

    # Plain fact or unknown
    return None, None, None
