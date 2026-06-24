"""Runtime Context Evaluator.

Evaluates retrieved memory atoms before they are used in a chat response or task.
Returns a structured ContextEvaluation that guides how the assistant uses memory.

Pipeline
--------
1. Deterministic pre-filter
   Hard rules applied without LLM:
   - Exclude superseded / deprecated / archived atoms (never active truth)
   - Exclude atoms below the relevance similarity threshold
   - Exclude atoms outside the current scope (when scope is specified)
   - Flag contested lifecycle atoms
   - Flag atoms with high disagreement scores
   - Flag atoms with low confidence
   - Flag possibly-stale atoms (older than STALE_DAYS threshold)

2. LLM critic (optional)
   Judges relevance, sufficiency, and appropriate caveats:
   - Does context actually answer the task?
   - Are any atoms irrelevant despite passing the pre-filter?
   - Are any atoms being over-applied?
   - Should the assistant verify / caveat / ask?

3. Risk gate
   Hard rules that override the critic:
   - No usable atoms → insufficient, caveat required
   - Unsafe content → refuse
   - Low aggregate confidence → upgrade to answer_with_caveat
   - Conflicting/contested atoms reduce confidence

Context status values
---------------------
  sufficient              — retrieved context adequately covers the task
  insufficient            — not enough relevant, trustworthy context
  stale                   — context may be outdated
  conflicting             — contradictory atoms in the retrieved set
  unsupported             — atoms have low confidence / support
  needs_verification      — verification recommended before relying on context
  needs_user_clarification — task is ambiguous; more info needed
  unsafe_or_blocked       — safety concern; refuse action

Final action values
-------------------
  answer               — proceed and answer
  answer_with_caveat   — answer but label uncertainty explicitly
  verify_then_answer   — should verify (e.g. web search) before answering
  ask_user             — request clarification first
  refuse               — safety block
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.db import get_store
from app.llm_provider import LLMProvider, get_llm_client
from app.memory_store import MemoryStore

# ── Constants ─────────────────────────────────────────────────────────────────

ALLOWED_STATUSES = frozenset({
    "sufficient",
    "insufficient",
    "stale",
    "conflicting",
    "unsupported",
    "needs_verification",
    "needs_user_clarification",
    "unsafe_or_blocked",
})

ALLOWED_ACTIONS = frozenset({
    "answer",
    "answer_with_caveat",
    "verify_then_answer",
    "ask_user",
    "refuse",
})

_STALE_DAYS = 180
_LOW_CONFIDENCE_THRESHOLD = 0.4
_LOW_SIMILARITY_THRESHOLD = 0.35
_HIGH_DISAGREEMENT_THRESHOLD = 0.4

_CRITIC_SYSTEM = (
    "You are a memory context evaluator for a personal knowledge assistant. "
    "Assess whether retrieved memory atoms provide sufficient, relevant, and "
    "trustworthy context for the given task. "
    "Return STRICT JSON only — no markdown, no explanation outside the JSON object."
)

_LIFECYCLE_IGNORE = frozenset({"superseded", "deprecated", "archived"})


# ── Decision dataclass ────────────────────────────────────────────────────────

@dataclass
class ContextEvaluation:
    task_summary: str
    retrieved_atom_ids: list[str]
    used_atom_ids: list[str]
    ignored_atom_ids: list[dict]       # [{atom_id, reason}]
    context_status: str
    confidence: float
    issues: list[dict]                  # [{type, atom_id, severity, resolution}]
    required_actions: list[str]
    final_action: str
    trace_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_summary": self.task_summary,
            "retrieved_atom_ids": self.retrieved_atom_ids,
            "used_atom_ids": self.used_atom_ids,
            "ignored_atom_ids": self.ignored_atom_ids,
            "context_status": self.context_status,
            "confidence": self.confidence,
            "issues": self.issues,
            "required_actions": self.required_actions,
            "final_action": self.final_action,
            "trace_id": self.trace_id,
        }


# ── Deterministic pre-filter ──────────────────────────────────────────────────

def _days_old(created_at_iso: str | None) -> float | None:
    if not created_at_iso:
        return None
    try:
        created = datetime.fromisoformat(created_at_iso.replace("Z", "+00:00"))
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - created).days
    except Exception:
        return None


def _deterministic_pre_filter(
    atoms: list[dict[str, Any]],
    current_scope: str | None,
) -> dict[str, Any]:
    """Apply hard lifecycle / quality / scope rules without LLM involvement.

    Returns:
        {
          "usable": [atom, ...],
          "ignored": [{"atom_id": ..., "reason": ...}, ...],
          "issues": [{"type": ..., "atom_id": ..., "severity": ..., "resolution": ...}, ...],
        }
    """
    usable: list[dict] = []
    ignored: list[dict] = []
    issues: list[dict] = []

    for atom in atoms:
        atom_id = str(atom.get("id", ""))
        lifecycle = str(atom.get("lifecycle_status") or "active").lower()
        confidence = float(atom.get("confidence") or 0.8)
        similarity = float(atom.get("similarity") or 0.0)
        disagreement = float(atom.get("disagreement_score") or 0.0)
        atom_scope = atom.get("scope")
        created_at = atom.get("created_at")

        # Hard ignore: inactive lifecycle states
        if lifecycle in _LIFECYCLE_IGNORE:
            ignored.append({"atom_id": atom_id, "reason": f"lifecycle_{lifecycle}"})
            continue

        # Hard ignore: below relevance threshold
        if similarity < _LOW_SIMILARITY_THRESHOLD:
            ignored.append({"atom_id": atom_id, "reason": "low_relevance"})
            continue

        # Hard ignore: scope mismatch (non-null, non-global, different scope)
        if (
            current_scope
            and atom_scope
            and atom_scope.lower() != "global"
            and atom_scope.lower() != current_scope.lower()
        ):
            ignored.append({"atom_id": atom_id, "reason": "scope_mismatch"})
            issues.append({
                "type": "scope_mismatch",
                "atom_id": atom_id,
                "severity": "low",
                "resolution": "ignored",
            })
            continue

        # Soft flags: contested lifecycle
        if lifecycle == "contested":
            issues.append({
                "type": "contested",
                "atom_id": atom_id,
                "severity": "medium",
                "resolution": "used_with_caveat",
            })

        # Soft flag: high disagreement in signals
        if disagreement > _HIGH_DISAGREEMENT_THRESHOLD:
            issues.append({
                "type": "conflicting_signals",
                "atom_id": atom_id,
                "severity": "medium",
                "resolution": "used_with_caveat",
            })

        # Soft flag: low confidence
        if confidence < _LOW_CONFIDENCE_THRESHOLD:
            issues.append({
                "type": "low_confidence",
                "atom_id": atom_id,
                "severity": "medium",
                "resolution": "used_with_caveat",
            })

        # Soft flag: possibly stale
        age = _days_old(created_at)
        if age is not None and age > _STALE_DAYS:
            issues.append({
                "type": "possibly_stale",
                "atom_id": atom_id,
                "severity": "low",
                "resolution": "used_with_caveat",
            })

        usable.append(atom)

    return {"usable": usable, "ignored": ignored, "issues": issues}


# ── LLM critic ────────────────────────────────────────────────────────────────

def _build_critic_prompt(
    task_summary: str,
    usable_atoms: list[dict[str, Any]],
    det_issues: list[dict[str, Any]],
) -> str:
    atom_payload = [
        {
            "id": a.get("id"),
            "content": a.get("content"),
            "memory_type": a.get("memory_type"),
            "scope": a.get("scope"),
            "confidence": round(float(a.get("confidence") or 0.8), 2),
            "similarity": round(float(a.get("similarity") or 0.0), 3),
            "lifecycle_status": a.get("lifecycle_status", "active"),
            "disagreement_score": round(float(a.get("disagreement_score") or 0.0), 2),
        }
        for a in usable_atoms
    ]
    ids = [str(a.get("id", "")) for a in usable_atoms]
    return (
        "Evaluate the retrieved memory atoms for the task below.\n\n"
        "Task:\n"
        + json.dumps(task_summary, ensure_ascii=False)
        + "\n\nUsable atoms (pre-filtered — superseded/archived already removed):\n"
        + json.dumps(atom_payload, ensure_ascii=False, indent=2)
        + "\n\nAlready-detected issues (from deterministic checks):\n"
        + json.dumps(det_issues, ensure_ascii=False, indent=2)
        + "\n\n"
        "Questions to answer:\n"
        "- Do the atoms actually address the task? If an atom is irrelevant, add it to ignored_atom_ids.\n"
        "- Is the context sufficient to answer without guessing?\n"
        "- Are any atoms possibly stale, over-applied, or used beyond their intended scope?\n"
        "- Should the assistant verify, caveat, or ask the user before answering?\n"
        "- Is any content unsafe or privacy-sensitive?\n\n"
        "Allowed context_status values:\n"
        "  sufficient | insufficient | stale | conflicting | unsupported\n"
        "  | needs_verification | needs_user_clarification | unsafe_or_blocked\n\n"
        "Allowed final_action values:\n"
        "  answer | answer_with_caveat | verify_then_answer | ask_user | refuse\n\n"
        "Guidelines:\n"
        "- Use 'answer' when atoms are relevant, OR when the question is conversational, "
        "philosophical, opinion-based, or clearly answerable from general knowledge — "
        "irrelevant atoms do not block answering.\n"
        "- Use 'answer_with_caveat' when atoms are partial, conflicting, or have quality issues "
        "that affect the answer's reliability.\n"
        "- Use 'verify_then_answer' only when external verification is specifically warranted "
        "for a factual claim.\n"
        "- Use 'ask_user' ONLY when the question itself is genuinely ambiguous or unclear — "
        "NOT simply because retrieved atoms are irrelevant. If atoms are irrelevant but the "
        "question is clear, use 'answer' instead.\n"
        "- Use 'refuse' only for clear safety/privacy violations.\n"
        "- Set confidence between 0.0 and 1.0 reflecting how much the atoms support the task.\n"
        "- If no atoms are relevant, set context_status='insufficient' and confidence<=0.3, "
        "but still set final_action='answer' unless the question itself is ambiguous.\n"
        f"- All atom IDs must come from this list: {ids}\n\n"
        "Return ONLY this JSON — no markdown fences:\n"
        "{\n"
        '  "context_status": "<status>",\n'
        '  "confidence": <0.0-1.0>,\n'
        '  "used_atom_ids": ["<id>", ...],\n'
        '  "ignored_atom_ids": [{"atom_id": "<id>", "reason": "<reason>"}, ...],\n'
        '  "additional_issues": [{"type": "<t>", "atom_id": "<id>", "severity": "<s>", "resolution": "<r>"}],\n'
        '  "final_action": "<action>",\n'
        '  "reasoning_summary": "<one concise sentence>"\n'
        "}"
    )


def _extract_json(text: str) -> dict[str, Any] | None:
    text = text.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        parsed = json.loads(text[start: end + 1])
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None


def _parse_critic_response(
    response: str,
    valid_ids: set[str],
    fallback_confidence: float,
) -> dict[str, Any]:
    parsed = _extract_json(response)
    if not parsed:
        return {
            "context_status": "insufficient",
            "confidence": fallback_confidence,
            "used_atom_ids": list(valid_ids),
            "ignored_atom_ids": [],
            "additional_issues": [{"type": "critic_parse_error", "severity": "low", "resolution": "used_all_atoms"}],
            "final_action": "answer_with_caveat",
        }

    status = str(parsed.get("context_status", "")).lower()
    if status not in ALLOWED_STATUSES:
        status = "insufficient"

    action = str(parsed.get("final_action", "")).lower()
    if action not in ALLOWED_ACTIONS:
        action = "answer_with_caveat"

    try:
        confidence = float(parsed.get("confidence", fallback_confidence))
        confidence = max(0.0, min(1.0, confidence))
    except (TypeError, ValueError):
        confidence = fallback_confidence

    # Sanitise atom ID lists — only accept IDs we actually retrieved
    raw_used = parsed.get("used_atom_ids") or []
    used_ids = [str(i) for i in raw_used if str(i) in valid_ids]
    if not used_ids and valid_ids:
        used_ids = list(valid_ids)

    raw_ignored = parsed.get("ignored_atom_ids") or []
    ignored = [
        {"atom_id": str(e.get("atom_id", "")), "reason": str(e.get("reason", "irrelevant"))}
        for e in raw_ignored
        if isinstance(e, dict) and str(e.get("atom_id", "")) in valid_ids
    ]

    extra_issues_raw = parsed.get("additional_issues") or []
    extra_issues = [
        {
            "type": str(i.get("type", "unknown")),
            "atom_id": str(i.get("atom_id", "")),
            "severity": str(i.get("severity", "low")),
            "resolution": str(i.get("resolution", "noted")),
        }
        for i in extra_issues_raw
        if isinstance(i, dict)
    ]

    return {
        "context_status": status,
        "confidence": confidence,
        "used_atom_ids": used_ids,
        "ignored_atom_ids": ignored,
        "additional_issues": extra_issues,
        "final_action": action,
    }


# ── Risk gate ─────────────────────────────────────────────────────────────────

def _apply_risk_gate(
    usable_count: int,
    det_issues: list[dict],
    critic: dict[str, Any],
) -> tuple[str, float, str]:
    """Return (final context_status, final confidence, final action).

    Hard rules applied in order of priority:
    1. Unsafe → refuse
    2. No usable atoms → insufficient
    3. Aggregate confidence penalties from issues
    4. If confidence too low → upgrade to caveat
    """
    status = critic.get("context_status", "insufficient")
    confidence = float(critic.get("confidence", 0.6))
    action = critic.get("final_action", "answer_with_caveat")

    # Hard override: unsafe
    if status == "unsafe_or_blocked":
        return "unsafe_or_blocked", 0.0, "refuse"

    # Hard override: nothing usable
    if usable_count == 0:
        confidence = max(confidence * 0.4, 0.2)
        return "insufficient", round(confidence, 3), "answer_with_caveat"

    # Count quality issues for confidence penalty
    penalty = 0.0
    for issue in det_issues:
        itype = issue.get("type", "")
        sev = issue.get("severity", "low")
        if itype in ("contested", "conflicting_signals"):
            penalty += 0.12 if sev == "high" else 0.08
        elif itype == "low_confidence":
            penalty += 0.05
        elif itype == "possibly_stale":
            penalty += 0.03

    all_issues = det_issues + (critic.get("additional_issues") or [])
    for issue in critic.get("additional_issues") or []:
        itype = issue.get("type", "")
        if itype in ("contested", "conflicting_signals"):
            penalty += 0.05

    confidence = max(0.1, confidence - penalty)
    confidence = round(confidence, 3)

    # If conflicting issues appear → set status to conflicting unless already worse
    conflict_count = sum(
        1 for i in all_issues
        if i.get("type") in ("contested", "conflicting_signals")
    )
    if conflict_count >= 2 and status == "sufficient":
        status = "conflicting"

    # If confidence drops below threshold, ensure caveat or verification
    if confidence < 0.5 and action == "answer":
        action = "answer_with_caveat"

    # If context is explicitly insufficient, always caveat
    if status in ("insufficient", "unsupported", "conflicting") and action == "answer":
        action = "answer_with_caveat"

    return status, confidence, action


# ── Evaluator class ───────────────────────────────────────────────────────────

class RuntimeContextEvaluator:
    """Evaluates retrieved memory context before use in a chat response.

    Usage::

        evaluator = RuntimeContextEvaluator()
        eval_result = evaluator.evaluate(task_message, retrieved_atoms, scope="my-project")

        # Use eval_result.used_atom_ids to filter memories for the prompt.
        # Store trace via store.store_context_trace(eval_result.to_dict()).
    """

    def __init__(
        self,
        store: MemoryStore | None = None,
        llm: LLMProvider | None = None,
    ) -> None:
        self.store = store if store is not None else get_store()
        self.llm = llm or get_llm_client()

    def evaluate(
        self,
        task_message: str,
        retrieved_atoms: list[dict[str, Any]],
        scope: str | None = None,
        use_critic: bool = False,
    ) -> ContextEvaluation:
        """Evaluate retrieved atoms for a given task.

        Parameters
        ----------
        task_message:
            The user message / task description.
        retrieved_atoms:
            Atoms returned by MemoryStore.retrieve_memories().
        scope:
            Current project/conversation scope (optional).
        use_critic:
            Whether to make an LLM critic call. Disable for speed-critical paths
            or testing where deterministic output is preferred.

        Returns
        -------
        ContextEvaluation with used_atom_ids, ignored_atom_ids, issues,
        context_status, confidence, final_action, and (if stored) trace_id.
        """
        task_summary = task_message[:300].strip()
        retrieved_ids = [str(a.get("id", "")) for a in retrieved_atoms]

        # ── Step 1: Deterministic pre-filter ─────────────────────────────────
        det = _deterministic_pre_filter(retrieved_atoms, scope)
        usable = det["usable"]
        ignored = list(det["ignored"])
        det_issues = list(det["issues"])

        # ── Step 2: LLM critic (optional) ────────────────────────────────────
        if use_critic and usable:
            valid_ids = {str(a.get("id", "")) for a in usable}
            fallback_conf = _default_confidence(usable, det_issues)
            critic_prompt = _build_critic_prompt(task_summary, usable, det_issues)
            try:
                critic_raw = self.llm.generate_response(
                    critic_prompt, system=_CRITIC_SYSTEM, json_mode=True
                )
                critic = _parse_critic_response(critic_raw, valid_ids, fallback_conf)
            except Exception as exc:
                critic = {
                    "context_status": "insufficient",
                    "confidence": fallback_conf,
                    "used_atom_ids": list(valid_ids),
                    "ignored_atom_ids": [],
                    "additional_issues": [
                        {"type": "critic_unavailable", "severity": "low",
                         "resolution": "used_all_atoms", "note": str(exc)}
                    ],
                    "final_action": "answer_with_caveat",
                }

            # Merge critic-identified ignores into our set
            for entry in critic.get("ignored_atom_ids") or []:
                aid = str(entry.get("atom_id", ""))
                if aid and aid not in {i["atom_id"] for i in ignored}:
                    ignored.append(entry)
            all_issues = det_issues + (critic.get("additional_issues") or [])
        else:
            # No critic: build a conservative result from deterministic data
            valid_ids = {str(a.get("id", "")) for a in usable}
            fallback_conf = _default_confidence(usable, det_issues)
            critic = {
                "context_status": "sufficient" if usable else "insufficient",
                "confidence": fallback_conf,
                "used_atom_ids": list(valid_ids),
                "ignored_atom_ids": [],
                "additional_issues": [],
                "final_action": "answer" if usable else "answer_with_caveat",
            }
            all_issues = det_issues

        # ── Step 3: Risk gate ─────────────────────────────────────────────────
        final_status, final_confidence, final_action = _apply_risk_gate(
            len(usable), all_issues, critic
        )

        # Final used atom IDs = critic's filtered set minus any critic-ignored
        critic_ignored_ids = {
            e.get("atom_id", "") for e in (critic.get("ignored_atom_ids") or [])
        }
        used_ids = [
            aid for aid in (critic.get("used_atom_ids") or list(valid_ids))
            if aid not in critic_ignored_ids
        ]

        # Required actions (informational list for the trace)
        required_actions: list[str] = []
        if final_action in ("verify_then_answer",):
            required_actions.append("external_verification")
        if final_action == "ask_user":
            required_actions.append("user_clarification")

        return ContextEvaluation(
            task_summary=task_summary,
            retrieved_atom_ids=retrieved_ids,
            used_atom_ids=used_ids,
            ignored_atom_ids=ignored,
            context_status=final_status,
            confidence=final_confidence,
            issues=all_issues,
            required_actions=required_actions,
            final_action=final_action,
        )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _default_confidence(usable: list[dict], issues: list[dict]) -> float:
    """Estimate baseline confidence from usable atom quality when no critic runs."""
    if not usable:
        return 0.25
    avg_sim = sum(float(a.get("similarity") or 0.0) for a in usable) / len(usable)
    avg_conf = sum(float(a.get("confidence") or 0.8) for a in usable) / len(usable)
    base = (avg_sim * 0.4 + avg_conf * 0.6)
    penalty = min(0.3, len(issues) * 0.04)
    return round(max(0.2, min(0.95, base - penalty)), 3)
