"""Response Draft Evaluator — the third core loop of the memory-layer system.

Runs after the LLM generates a draft answer and before the answer reaches the
user. Checks whether the draft:

  - Followed the required final_action from the RuntimeContextEvaluator
  - Overstated confidence given weak / conflicting / stale context
  - Missed required epistemic labels (best guess, unverified, etc.)
  - Should trigger an actual verification pass (verify_then_answer)
  - Refused / asked the user when it should have
  - Contains durable lessons worth committing to the Memory Commit Pipeline

Three LLM passes are possible per turn (all non-fatal):
  1. Critic  — always runs; returns verdict + issues + commit_candidates
  2. Verification pass — only when final_action == verify_then_answer or
                         critic verdict == needs_verification
  3. Revision pass     — only when verdict in (needs_revision, blocked) and
                         critic did not supply a revised_answer

Integration: called from app/chat.py after the draft is generated.
Trace stored in runtime_response_traces via MemoryStore.store_response_trace().
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

RESPONSE_VERDICTS = frozenset({
    "approved",            # answer is correct and appropriately hedged
    "needs_caveat",        # content is fine but missing epistemic labels
    "needs_revision",      # content overstates, uses ignored memory, or is wrong
    "needs_verification",  # final_action was verify_then_answer; no verification done
    "blocked",             # final_action was refuse/ask_user but answer guessed
})

OVERSTATEMENT_LEVELS = frozenset({"none", "low", "medium", "high"})


@dataclass
class ResponseEvaluation:
    verdict: str
    action_followed: bool
    overstatement_risk: str
    issues: list[dict]
    revised_answer: str | None
    commit_candidates: list[str]
    reasoning: str
    trace_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "action_followed": self.action_followed,
            "overstatement_risk": self.overstatement_risk,
            "issues": self.issues,
            "revised_answer": self.revised_answer,
            "commit_candidates": self.commit_candidates,
            "reasoning": self.reasoning,
            "trace_id": self.trace_id,
        }


# ── Deterministic pre-checks ──────────────────────────────────────────────────

def _hard_override(draft: str, final_action: str) -> str | None:
    """Return a forced verdict if the draft violates a hard rule, else None."""
    if final_action == "refuse":
        # Any substantive answer when we should refuse is blocked
        return "blocked"
    if final_action == "ask_user":
        # Draft must contain a clarifying question; if not, force revision
        if "?" not in draft:
            return "needs_revision"
    return None


# ── LLM critic ────────────────────────────────────────────────────────────────

def _build_critic_prompt(
    user_msg: str,
    draft: str,
    ce_summary: dict,
    used_memories: list[dict],
) -> str:
    # Separate trigger atoms (correction/warning/contested) from regular atoms.
    trigger_lines = []
    context_lines = []
    for m in used_memories[:8]:
        sid = str(m.get("id", ""))[:8]
        snippet = (m.get("context_summary") or m.get("content", ""))[:140]
        mtype = str(m.get("memory_type", "")).lower()
        lifecycle = str(m.get("lifecycle_status", "")).lower()
        dscore = float(m.get("disagreement_score") or 0.0)
        if mtype in ("correction", "warning") or lifecycle == "contested" or dscore > 0.4:
            trigger_lines.append(f"  - [{sid}] [{mtype}] {snippet}")
        else:
            context_lines.append(f"  - [{sid}] {snippet}")

    trigger_text = "\n".join(trigger_lines) or "  (none)"
    context_text = "\n".join(context_lines) or "  (none)"

    return f"""You are a correction-checking critic for an AI assistant with a persistent memory layer.

The assistant retrieved memory atoms that flag known corrections, warnings, or contested beliefs.
Your job: check whether the draft answer handles those correctly.

=== Trigger Atoms (corrections / warnings / contested) ===
{trigger_text}

=== Supporting Context Atoms ===
{context_text}

=== User Message ===
{user_msg[:500]}

=== Draft Answer ===
{draft[:1500]}

=== Instructions ===
Focus on whether the draft contradicts or ignores the trigger atoms.
Do NOT penalise the draft for hedging appropriately.

Return a JSON object with EXACTLY these fields:
{{
  "verdict": "approved" | "needs_caveat" | "needs_revision",
  "action_followed": true | false,
  "overstatement_risk": "none" | "low" | "medium" | "high",
  "issues": [
    {{"type": "contradiction_of_correction" | "ignores_warning" | "overstatement" | "missing_caveat" | "invented_fact", "detail": "...", "severity": "low" | "medium" | "high"}}
  ],
  "revised_answer": null or a full revised answer string (only when verdict == "needs_revision"),
  "commit_candidates": ["...durable lesson worth storing as a memory atom..."],
  "reasoning": "1–2 sentence verdict summary"
}}

Verdict rules:
  approved        — draft is consistent with all trigger atoms; appropriately hedged
  needs_caveat    — draft is correct but should acknowledge the correction/warning context
  needs_revision  — draft contradicts a stored correction, ignores a warning, or fabricates facts

Return ONLY the JSON object. No markdown, no preamble, no explanation outside the JSON."""


def _parse_critic_response(raw: str, draft: str) -> dict[str, Any]:
    """Parse LLM critic JSON. Returns a safe fallback on any error."""
    cleaned = re.sub(r"^```[a-z]*\n?|\n?```$", "", raw.strip(), flags=re.MULTILINE).strip()
    m = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if not m:
        return _fallback(draft)
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return _fallback(draft)

    verdict = data.get("verdict", "needs_caveat")
    if verdict not in RESPONSE_VERDICTS:
        # Map removed verdicts to their nearest equivalent for backward compat.
        verdict = "needs_revision" if verdict in ("needs_verification", "blocked") else "needs_caveat"

    overstatement = data.get("overstatement_risk", "low")
    if overstatement not in OVERSTATEMENT_LEVELS:
        overstatement = "low"

    return {
        "verdict": verdict,
        "action_followed": bool(data.get("action_followed", False)),
        "overstatement_risk": overstatement,
        "issues": [
            i for i in data.get("issues", [])
            if isinstance(i, dict)
        ][:10],
        "revised_answer": data.get("revised_answer") or None,
        "commit_candidates": [
            c for c in data.get("commit_candidates", [])
            if isinstance(c, str) and len(c) > 10
        ][:5],
        "reasoning": str(data.get("reasoning", ""))[:400],
    }


def _fallback(draft: str) -> dict[str, Any]:
    return {
        "verdict": "needs_caveat",
        "action_followed": False,
        "overstatement_risk": "low",
        "issues": [{"type": "critic_parse_error",
                    "detail": "Critic response could not be parsed.",
                    "severity": "low"}],
        "revised_answer": None,
        "commit_candidates": [],
        "reasoning": "Critic parse failed; conservative fallback applied.",
    }


# ── Revision and verification passes ─────────────────────────────────────────

def _run_verification_pass(
    user_msg: str,
    draft: str,
    used_memories: list[dict],
    llm: Any,
) -> str:
    """Re-run the LLM to label each claim against available memory context.

    Returns a revised answer with unsupported claims labelled, or the original
    draft if the verification pass fails or produces nothing useful.
    """
    mem_lines = []
    for m in used_memories[:6]:
        sid = str(m.get("id", ""))[:8]
        snippet = (m.get("context_summary") or m.get("content", ""))[:140]
        mem_lines.append(f"  [{sid}] {snippet}")
    mem_text = "\n".join(mem_lines) or "  (none)"

    prompt = (
        "You are a fact-checking assistant. Review the following answer against "
        "the available memory context and verify each factual claim.\n\n"
        "Rules:\n"
        "  - If a claim is clearly supported by a retrieved memory entry, keep it as-is.\n"
        "  - If a claim is plausible but not supported, prefix it with 'best guess:' or 'unverified:'.\n"
        "  - If a claim contradicts available memory, correct it and note the correction inline.\n"
        "  - Do not add a preamble. Return only the revised answer text.\n\n"
        f"Available memory context:\n{mem_text}\n\n"
        f"Original question: {user_msg[:400]}\n\n"
        f"Answer to verify:\n{draft[:1500]}\n"
    )
    try:
        verified = llm.generate_response(
            prompt,
            system=(
                "You are a precise fact-checking assistant. "
                "Return only the verified/revised answer text."
            ),
        ).strip()
        return verified if verified and len(verified) > 20 else draft
    except Exception:
        return draft


def _run_revision_pass(
    user_msg: str,
    draft: str,
    issues: list[dict],
    final_action: str,
    llm: Any,
) -> str:
    """Generate a revised answer that addresses identified quality issues.

    For refuse/ask_user actions this produces the appropriate substitute
    (refusal message or clarifying question). For other verdicts it rewrites
    the draft to fix overstatements and add missing epistemic labels.
    """
    if final_action == "ask_user":
        prompt = (
            "The user's question requires clarification before a confident answer "
            "can be given. Write a polite, concise clarifying question (1–2 sentences) "
            "that asks the user for the specific missing information. "
            "Do not attempt to answer the original question. "
            "Return only the clarifying question.\n\n"
            f"Original user message: {user_msg[:500]}\n"
        )
    elif final_action == "refuse":
        prompt = (
            "The system determined this request should be declined because retrieved "
            "context is insufficient, unsafe, or unverifiable. "
            "Write a polite, brief refusal (1–2 sentences) explaining why a confident "
            "answer is not possible right now and what the user could do instead "
            "(e.g. provide more context, or try rephrasing). "
            "Do not attempt to answer the original question. "
            "Return only the refusal message.\n\n"
            f"Original user message: {user_msg[:500]}\n"
        )
    else:
        issue_lines = "\n".join(
            f"  - {i.get('type', '?')} ({i.get('severity', '?')}): {i.get('detail', '')}"
            for i in issues[:6]
        ) or "  - overstatement or missing epistemic labels"

        prompt = (
            "Revise the following answer to fix the quality issues listed below.\n\n"
            "Rules:\n"
            "  - Prefix unsupported claims with 'best guess:', 'unverified:', or 'weakly supported:'.\n"
            "  - Remove or soften overstatements.\n"
            "  - Add a brief caveat at the end if overall confidence is low.\n"
            "  - Preserve all content that is well-supported; do not strip useful information.\n"
            "  - Return only the revised answer.\n\n"
            f"Quality issues to fix:\n{issue_lines}\n\n"
            f"Original question: {user_msg[:400]}\n\n"
            f"Draft answer:\n{draft[:1500]}\n"
        )
    try:
        revised = llm.generate_response(
            prompt,
            system="You are a precise answer-quality editor. Return only the revised answer.",
        ).strip()
        return revised if revised and len(revised) > 10 else draft
    except Exception:
        return draft


# ── Evaluator ─────────────────────────────────────────────────────────────────

class ResponseEvaluator:
    """Evaluates a draft LLM answer against the context evaluation result.

    Three potential LLM passes:
      1. Critic      — always (unless hard override triggers early return)
      2. Verification — if final_action == verify_then_answer
      3. Revision    — if verdict requires it and critic didn't provide one

    All passes are non-fatal. Failures fall back gracefully.
    """

    def __init__(self, llm: Any = None) -> None:
        from app.llm_provider import get_llm_client
        self._llm = llm or get_llm_client()

    def evaluate(
        self,
        draft_answer: str,
        context_eval: Any,                   # ContextEvaluation | None
        used_memories: list[dict],
        user_msg: str,
        use_critic: bool = True,
    ) -> ResponseEvaluation:
        """Evaluate the draft and return a ResponseEvaluation.

        If use_critic=False, skips the LLM critic call (for deterministic tests).
        Hard overrides (refuse / ask_user) still apply regardless of use_critic.
        """
        final_action = getattr(context_eval, "final_action", "answer") if context_eval else "answer"
        ce_summary: dict[str, Any]
        if context_eval is not None:
            ce_summary = {
                "context_status": getattr(context_eval, "context_status", "unknown"),
                "confidence": getattr(context_eval, "confidence", 0.5),
                "final_action": final_action,
                "issues": getattr(context_eval, "issues", []),
            }
        else:
            ce_summary = {
                "context_status": "unknown",
                "confidence": 0.5,
                "final_action": "answer",
                "issues": [],
            }

        # ── Step 1: Hard override check ───────────────────────────────────────
        hard_verdict = _hard_override(draft_answer, final_action)

        if hard_verdict == "blocked":
            revised = _run_revision_pass(user_msg, draft_answer, [], final_action, self._llm)
            return ResponseEvaluation(
                verdict="blocked",
                action_followed=False,
                overstatement_risk="none",
                issues=[{
                    "type": "wrong_action",
                    "detail": f"final_action='{final_action}' required but answer attempted a direct response.",
                    "severity": "high",
                }],
                revised_answer=revised,
                commit_candidates=[],
                reasoning=f"Hard override: final_action='{final_action}' requires a different response type.",
            )

        # ── Step 2: LLM critic ────────────────────────────────────────────────
        if use_critic:
            critic_prompt = _build_critic_prompt(user_msg, draft_answer, ce_summary, used_memories)
            try:
                raw = self._llm.generate_response(
                    critic_prompt,
                    system="You are a strict response-quality critic. Return only valid JSON.",
                )
                parsed = _parse_critic_response(raw, draft_answer)
            except Exception:
                parsed = _fallback(draft_answer)
        else:
            # Deterministic path: no LLM call; derive verdict from context_eval
            parsed = _deterministic_verdict(draft_answer, final_action, ce_summary)

        verdict: str = parsed["verdict"]
        revised_answer: str | None = parsed.get("revised_answer")

        # Respect the hard_override even if critic disagrees
        if hard_verdict == "needs_revision" and verdict not in ("needs_revision", "blocked"):
            verdict = "needs_revision"
            parsed["issues"].append({
                "type": "wrong_action",
                "detail": (
                    f"final_action='{final_action}' requires clarification "
                    "but answer did not include a question."
                ),
                "severity": "medium",
            })

        # ── Step 3: Verification pass ─────────────────────────────────────────
        # Run if: critic said needs_verification OR final_action was verify_then_answer
        # and the critic approved/caveat (meaning the draft didn't self-verify)
        needs_verify = (
            verdict == "needs_verification"
            or (
                final_action == "verify_then_answer"
                and verdict in ("approved", "needs_caveat")
            )
        )
        if needs_verify and use_critic:
            verified = _run_verification_pass(user_msg, draft_answer, used_memories, self._llm)
            if verified != draft_answer:
                revised_answer = verified
                verdict = "needs_verification"

        # ── Step 4: Revision pass if still needed ────────────────────────────
        if verdict in ("needs_revision", "blocked") and not revised_answer:
            revised_answer = _run_revision_pass(
                user_msg, draft_answer, parsed["issues"], final_action, self._llm
            )

        return ResponseEvaluation(
            verdict=verdict,
            action_followed=parsed["action_followed"],
            overstatement_risk=parsed["overstatement_risk"],
            issues=parsed["issues"],
            revised_answer=revised_answer,
            commit_candidates=parsed["commit_candidates"],
            reasoning=parsed["reasoning"],
        )


# ── Deterministic fallback verdict (no LLM) ───────────────────────────────────

def _deterministic_verdict(
    draft: str,
    final_action: str,
    ce_summary: dict,
) -> dict[str, Any]:
    """Derive a conservative verdict without calling the LLM.

    Used when use_critic=False (e.g. in unit tests).
    """
    confidence = float(ce_summary.get("confidence", 0.5))
    context_status = ce_summary.get("context_status", "unknown")

    if final_action == "ask_user" and "?" not in draft:
        return {
            "verdict": "needs_revision",
            "action_followed": False,
            "overstatement_risk": "low",
            "issues": [{"type": "wrong_action",
                        "detail": "ask_user required but no question in draft",
                        "severity": "medium"}],
            "revised_answer": None,
            "commit_candidates": [],
            "reasoning": "Deterministic: ask_user action not honoured.",
        }

    if context_status in ("conflicting", "insufficient", "stale") or confidence < 0.45:
        return {
            "verdict": "needs_caveat",
            "action_followed": final_action in ("answer_with_caveat", "verify_then_answer"),
            "overstatement_risk": "medium" if confidence < 0.45 else "low",
            "issues": [{"type": "missing_caveat",
                        "detail": f"context_status={context_status}, confidence={confidence:.2f}",
                        "severity": "medium"}],
            "revised_answer": None,
            "commit_candidates": [],
            "reasoning": "Deterministic: weak context warrants caveat labels.",
        }

    return {
        "verdict": "approved",
        "action_followed": True,
        "overstatement_risk": "none",
        "issues": [],
        "revised_answer": None,
        "commit_candidates": [],
        "reasoning": "Deterministic: context is sufficient and action appears correct.",
    }
