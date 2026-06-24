"""Memory Commit Pipeline.

Every memory candidate must pass through this pipeline before being committed
to durable storage. The pipeline ensures that memories are:

  1. Reconciled against existing atoms (duplicate / refinement / conflict)
  2. Reviewed by a critic LLM (durability, clarity, sensitivity, quality)
  3. Risk-gated via explicit decision rules
  4. Written only after a structured decision is reached
  5. Traced in memory_commit_traces for audit and dashboard visibility

Decision values
---------------
  commit              — new atom created and stored
  refine_existing     — new atom stored, matched atom(s) superseded
  supersede_existing  — alias for refine_existing with a stronger replacement
  reinforce_existing  — reinforcement signal added to existing atom; no new atom
  mark_conflict       — queued to memory_proposals with relationship=conflict
  propose_for_review  — queued to memory_proposals (human approval required)
  reject              — no write; trace records the rejection reason
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from app.llm_provider import LLMProvider, get_llm_client
from app.memory_store import MemoryStore, REVISABLE_TYPES
from app.sqlite_store import SQLiteStore as _SQLiteStore
from app.reconciliation import CandidateReconciler

# Scope must match type:name format (e.g. project:foo, model:bar) or be a known
# single-word canonical value.  Anything else is treated as malformed.
_VALID_SCOPE_RE = re.compile(r'^[a-z][a-z0-9_-]*:[a-z][a-z0-9_:.-]+$')
_KNOWN_SINGLE_SCOPES = frozenset({"user", "global"})

ALLOWED_DECISIONS = frozenset({
    "commit",
    "refine_existing",
    "supersede_existing",
    "reinforce_existing",
    "mark_conflict",
    "propose_for_review",
    "reject",
})

_ALLOWED_REJECTION_REASONS = frozenset({
    "too_vague",
    "duplicate",
    "sensitive",
    "temporary",
    "obvious",
    "incoherent",
})

_CRITIC_SYSTEM = (
    "You are a memory critic for an epistemic belief system. Your job is to classify "
    "and reframe content for storage — not to reject it. Every signal has value: "
    "wrong facts reveal user beliefs, vague content reveals uncertainty, ephemeral "
    "content reveals temporal context. You reject ONLY secrets/credentials and "
    "incoherent noise. Everything else is stored with appropriate type and confidence. "
    "Return STRICT JSON only — no markdown, no explanation outside the JSON object."
)


# ── Decision dataclass ────────────────────────────────────────────────────────

@dataclass
class CommitDecision:
    decision: str
    candidate_text: str
    final_memory_text: str | None
    scope: str | None
    memory_type: str
    confidence: float
    lifecycle_action: str
    duplicate_atom_ids: list[str] = field(default_factory=list)
    reinforces_atom_ids: list[str] = field(default_factory=list)
    refines_atom_ids: list[str] = field(default_factory=list)
    supersedes_atom_ids: list[str] = field(default_factory=list)
    conflicts_with_atom_ids: list[str] = field(default_factory=list)
    critic_notes: list[str] = field(default_factory=list)
    write_action: str = "none"
    committed_atom_id: str | None = None
    committed_signal_id: str | None = None
    proposal_id: str | None = None
    rejection_reason: str | None = None
    trace_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "candidate_text": self.candidate_text,
            "final_memory_text": self.final_memory_text,
            "scope": self.scope,
            "memory_type": self.memory_type,
            "confidence": self.confidence,
            "lifecycle_action": self.lifecycle_action,
            "duplicate_atom_ids": self.duplicate_atom_ids,
            "reinforces_atom_ids": self.reinforces_atom_ids,
            "refines_atom_ids": self.refines_atom_ids,
            "supersedes_atom_ids": self.supersedes_atom_ids,
            "conflicts_with_atom_ids": self.conflicts_with_atom_ids,
            "critic_notes": self.critic_notes,
            "write_action": self.write_action,
            "committed_atom_id": self.committed_atom_id,
            "committed_signal_id": self.committed_signal_id,
            "proposal_id": self.proposal_id,
            "rejection_reason": self.rejection_reason,
            "trace_id": self.trace_id,
        }


# ── Critic prompt builder ─────────────────────────────────────────────────────

def _build_critic_prompt(
    candidate_text: str,
    memory_type: str,
    scope: str | None,
    confidence: float,
    reconciler_relationship: str,
    reconciler_reason: str,
    related_atoms: list[dict[str, Any]],
    reframe_hint: str | None = None,
    suggested_memory_type: str | None = None,
) -> str:
    related_payload = [
        {
            "id": a.get("id"),
            "content": a.get("content"),
            "memory_type": a.get("memory_type"),
            "scope": a.get("scope"),
            "lifecycle_status": a.get("lifecycle_status", "active"),
            "similarity": round(float(a.get("similarity", 0.0)), 3),
        }
        for a in related_atoms
    ]
    classifier_hints = {}
    if suggested_memory_type:
        classifier_hints["suggested_memory_type"] = suggested_memory_type
    if reframe_hint:
        classifier_hints["reframe_hint"] = reframe_hint

    return (
        "Classify and store this memory candidate in an epistemic belief system.\n\n"
        "Candidate:\n"
        + json.dumps(
            {
                "content": candidate_text,
                "memory_type": memory_type,
                "scope": scope,
                "confidence": confidence,
            },
            ensure_ascii=False,
            indent=2,
        )
        + (
            "\n\nClassifier hints (from write quality pre-gate):\n"
            + json.dumps(classifier_hints, ensure_ascii=False, indent=2)
            if classifier_hints else ""
        )
        + "\n\nReconciler verdict:\n"
        + json.dumps(
            {"relationship": reconciler_relationship, "reason": reconciler_reason},
            ensure_ascii=False,
            indent=2,
        )
        + "\n\nRelated existing atoms:\n"
        + json.dumps(related_payload, ensure_ascii=False, indent=2)
        + "\n\n"
        "Epistemic storage rules:\n"
        "- NEVER reject for quality reasons (vague, short, obvious, temporary). "
        "These are valid epistemic signals stored at lower confidence.\n"
        "- ONLY reject for: secrets/credentials (sensitive) or total incoherence (incoherent).\n"
        "- Unverified user claims → rewrite as 'The user believes/asserts X.' "
        "Set memory_type=belief, confidence 0.3–0.45.\n"
        "- Vague or uncertain content → rewrite as attributed belief. "
        "Do NOT reject for vagueness.\n"
        "- Ephemeral/temporal content → rewrite to preserve the temporal context: "
        "'As of [time], user reported X.' Set memory_type=observation.\n"
        "- User intent or to-do → rewrite as 'The user intends/wants X.' "
        "Set memory_type=preference.\n"
        "- Obvious canonical facts (sky is blue, water is wet) → store at confidence 0.9 as "
        "baseline reference. They anchor belief-deviation detection: when users claim otherwise, "
        "the contrast is meaningful.\n"
        "- Questions from users → rewrite as 'The user is inquiring about X.' "
        "Set memory_type=observation, confidence 0.5.\n"
        "- Architectural decisions, policies, tech constraints → store as-is at full confidence.\n"
        "- Relationship respect: if reconciler says duplicate/reinforcement → reinforce_existing.\n"
        "- If reconciler says refinement → refine_existing.\n"
        "- If reconciler says conflict/opinion_change → mark_conflict or supersede_existing.\n"
        "- Always rewrite final_memory_text into a clean, self-contained statement. "
        "Never use 'I', 'we', 'today', or session-internal references.\n\n"
        "Allowed decisions:\n"
        "  commit | refine_existing | supersede_existing | "
        "reinforce_existing | mark_conflict | propose_for_review | reject\n\n"
        "Allowed rejection_reason (only when decision=reject):\n"
        "  sensitive | incoherent\n\n"
        "Return ONLY this JSON — no markdown fences:\n"
        "{\n"
        '  "decision": "<decision>",\n'
        '  "final_memory_text": "<cleaned statement, or null if rejecting>",\n'
        '  "memory_type": "<type>",\n'
        '  "scope": "<scope or null>",\n'
        '  "confidence": <0.0-1.0>,\n'
        '  "critic_notes": ["<note>"],\n'
        '  "context_summary": "<1-2 sentences: when and why this fact is relevant>",\n'
        '  "rejection_reason": null,\n'
        '  "novelty_score": <0.0-1.0>,\n'
        '  "interest_flag": <true|false>,\n'
        '  "suggested_visibility": "public" | "private"\n'
        "}\n\n"
        "novelty_score: how unusual, counterintuitive, or potentially important is this idea "
        "beyond its immediate context? 0=mundane/common, 1=highly novel or paradigm-challenging. "
        "interest_flag: set true only when novelty_score >= 0.75.\n\n"
        "suggested_visibility rules (STRICT):\n"
        '  "private" — content contains ANY of: passwords, API keys, tokens, credentials, '
        "personal health/medical info, home addresses, private financial details, "
        "identifiable personal data about a named individual, internal secrets.\n"
        '  "public" — everything else: facts, decisions, opinions, architectural choices, '
        "general knowledge, beliefs about the world, project patterns, observations about tools or models.\n"
        "When in doubt, prefer public. The caller's requested visibility is overridden only when "
        "you detect genuinely sensitive content that must stay private."
    )


# ── JSON parser ───────────────────────────────────────────────────────────────

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
    fallback_candidate: str,
    fallback_type: str,
    fallback_scope: str | None,
    fallback_confidence: float,
) -> dict[str, Any]:
    parsed = _extract_json(response)
    if not parsed:
        return {
            "decision": "propose_for_review",
            "final_memory_text": fallback_candidate,
            "memory_type": fallback_type,
            "scope": fallback_scope,
            "confidence": fallback_confidence,
            "critic_notes": ["Critic response unparseable; sent to review."],
            "rejection_reason": None,
        }

    decision = str(parsed.get("decision", "")).strip().lower()
    if decision not in ALLOWED_DECISIONS:
        decision = "propose_for_review"

    raw_text = parsed.get("final_memory_text")
    final_text = str(raw_text).strip() if raw_text else fallback_candidate

    memory_type = (
        str(parsed.get("memory_type") or fallback_type).strip().lower()
        or fallback_type
    )
    scope_val = parsed.get("scope")
    scope = str(scope_val).strip() if scope_val else fallback_scope
    # Normalize common critic aliases to canonical scope format
    if scope:
        _scope_lower = scope.lower()
        if _scope_lower in {
            "memory-layer-project", "memory layer project", "memory_layer_project",
            "this project", "the project", "project", "memory-layer",
        }:
            scope = "project:memory-layer"
        elif (
            _scope_lower not in _KNOWN_SINGLE_SCOPES
            and not _VALID_SCOPE_RE.match(_scope_lower)
        ):
            # Malformed scope — fall back to what extraction already set
            scope = fallback_scope
        else:
            scope = _scope_lower  # normalise casing

    try:
        confidence = float(parsed.get("confidence", fallback_confidence))
        confidence = max(0.0, min(1.0, confidence))
    except (TypeError, ValueError):
        confidence = fallback_confidence

    notes_raw = parsed.get("critic_notes", [])
    notes = (
        [str(n).strip() for n in notes_raw if n]
        if isinstance(notes_raw, list)
        else []
    )

    rej_raw = parsed.get("rejection_reason")
    rejection_reason = str(rej_raw).strip() if rej_raw else None
    if rejection_reason and rejection_reason not in _ALLOWED_REJECTION_REASONS:
        rejection_reason = "too_vague"

    context_summary_raw = parsed.get("context_summary")
    context_summary = str(context_summary_raw).strip() if context_summary_raw else None

    return {
        "decision": decision,
        "final_memory_text": final_text,
        "memory_type": memory_type,
        "scope": scope,
        "confidence": confidence,
        "critic_notes": notes,
        "rejection_reason": rejection_reason,
        "context_summary": context_summary,
    }


# ── Risk gate ─────────────────────────────────────────────────────────────────

def _apply_risk_gate(
    reconciler_rel: str,
    matched_ids: list[str],
    critic_decision: str,
) -> tuple[str, str]:
    """Combine reconciler and critic signals into a final (decision, lifecycle_action).

    Hard rules that critic cannot override:
    - duplicate/reinforcement → always reinforce_existing (never create a duplicate atom)
    - sensitivity/vagueness/obviousness → reject always honoured
    """
    # Hard override: duplicate/reinforcement → reinforce (even if critic says commit)
    if reconciler_rel in ("duplicate", "reinforcement"):
        return "reinforce_existing", "signal_added"

    # Critic rejection is always honoured for other relationship types
    if critic_decision == "reject":
        return "reject", "none"

    # Conflict / opinion_change
    if reconciler_rel in ("conflict", "opinion_change"):
        if critic_decision in ("supersede_existing", "refine_existing"):
            return "supersede_existing", "superseded"
        return "mark_conflict", "proposed"

    # Refinement
    if reconciler_rel == "refinement":
        if critic_decision in ("commit", "refine_existing", "supersede_existing"):
            return "refine_existing", "superseded"
        if critic_decision == "propose_for_review":
            return "propose_for_review", "proposed"
        return "reject", "none"

    # New (or unrecognised relationship treated as new)
    if critic_decision == "commit":
        return "commit", "created"
    if critic_decision in ("refine_existing", "supersede_existing"):
        return ("refine_existing", "superseded") if matched_ids else ("commit", "created")
    if critic_decision == "reinforce_existing":
        return ("reinforce_existing", "signal_added") if matched_ids else ("commit", "created")
    if critic_decision == "propose_for_review":
        return "propose_for_review", "proposed"
    if critic_decision == "mark_conflict":
        return "mark_conflict", "proposed"

    return "propose_for_review", "proposed"


# ── Visibility resolver ───────────────────────────────────────────────────────

def _effective_visibility(caller_visibility: str, critic: dict) -> str:
    """Critic can force private for sensitive content; otherwise caller wins."""
    if critic.get("suggested_visibility") == "private":
        return "private"
    return caller_visibility or "private"


# ── Pipeline class ────────────────────────────────────────────────────────────

class MemoryCommitPipeline:
    """Central commit pipeline for all memory writes.

    Usage::

        pipeline = MemoryCommitPipeline()
        decision = pipeline.commit_candidate(candidate_dict)
        print(decision.to_dict())
    """

    def __init__(
        self,
        store: "MemoryStore | _SQLiteStore | None" = None,
        llm: LLMProvider | None = None,
    ) -> None:
        if store is None:
            from app.db import get_store
            store = get_store()
        self.store = store
        self.llm = llm or get_llm_client()
        self._reconciler = CandidateReconciler(store=self.store, ollama=self.llm)

    def commit_candidate(
        self,
        candidate: dict[str, Any],
        source_key: str = "local_user",
        source_type: str = "local",
        source_user_id: str | None = None,
        visibility: str = "private",
    ) -> CommitDecision:
        """Run a candidate through the full commit pipeline.

        Steps:
          1. Reconcile vs existing atoms
          2. Critic review + canonicalize
          3. Risk gate
          4. Write action
          5. Store commit trace

        source_key / source_type are stored on memory_signals for provenance.
        Use source_key to identify the originating LLM or tool
        (e.g. "github_copilot", "gpt-4o", "claude-3-7", "local_user").
        The candidate dict may also carry "source_key" / "source_type" fields;
        explicit params take priority over dict values.

        Returns a CommitDecision with all context and the trace ID.
        Raises ValueError if candidate has no content.
        """
        content = str(candidate.get("content", "")).strip()
        if not content:
            raise ValueError("Candidate has no content.")

        memory_type = str(candidate.get("memory_type") or "fact").lower()
        scope_val = candidate.get("scope")
        scope = str(scope_val).strip() if scope_val else None
        confidence = float(candidate.get("confidence") or 0.8)
        importance = float(candidate.get("importance") or 0.5)
        # Allow candidate dict to carry source info; explicit params take priority
        effective_source_key = candidate.get("source_key") or source_key
        effective_source_type = candidate.get("source_type") or source_type

        # ── Step 1: Reconcile ─────────────────────────────────────────────────
        try:
            reconciliation = self._reconciler.reconcile(
                content=content,
                memory_type=memory_type,
                scope=scope,
                retrieve_limit=5,
            )
        except Exception as exc:
            reconciliation = {
                "relationship": "new",
                "matched_memory_ids": [],
                "reason": f"Reconciliation failed: {exc}",
                "recommended_action": "ask_user",
                "related_memories": [],
            }

        reconciler_rel = str(reconciliation.get("relationship", "new")).lower()
        reconciler_reason = reconciliation.get("reason", "")
        matched_ids: list[str] = reconciliation.get("matched_memory_ids") or []
        related_memories: list[dict] = reconciliation.get("related_memories") or []

        # Honour update_existing recommended_action
        rec_action = reconciliation.get("recommended_action", "")
        if (
            rec_action == "update_existing"
            and reconciler_rel not in ("refinement", "new")
        ):
            reconciler_rel = "refinement"

        # ── Step 2: Critic review + canonicalize ──────────────────────────────
        critic_prompt = _build_critic_prompt(
            candidate_text=content,
            memory_type=memory_type,
            scope=scope,
            confidence=confidence,
            reconciler_relationship=reconciler_rel,
            reconciler_reason=reconciler_reason,
            related_atoms=related_memories,
            reframe_hint=candidate.get("reframe_hint"),
            suggested_memory_type=candidate.get("suggested_memory_type"),
        )
        try:
            critic_raw = self.llm.generate_response(
                critic_prompt,
                system=_CRITIC_SYSTEM,
                json_mode=True,
            )
            critic = _parse_critic_response(
                critic_raw, content, memory_type, scope, confidence
            )
        except Exception as exc:
            critic = {
                "decision": "propose_for_review",
                "final_memory_text": content,
                "memory_type": memory_type,
                "scope": scope,
                "confidence": confidence,
                "critic_notes": [f"Critic unavailable: {exc}; sent to review."],
                "rejection_reason": None,
            }

        # ── Step 3: Risk gate ─────────────────────────────────────────────────
        final_decision, lifecycle_action = _apply_risk_gate(
            reconciler_rel, matched_ids, critic["decision"]
        )

        final_text: str = critic.get("final_memory_text") or content
        final_type: str = critic.get("memory_type") or memory_type
        final_scope: str | None = critic.get("scope") or scope
        final_conf: float = float(critic.get("confidence") or confidence)
        # Pre-fetch prior atom state for revisable types — needed for belief revision log.
        # Must happen before any write so we capture the BEFORE state.
        _prior_atom_state: dict | None = None
        if final_type in REVISABLE_TYPES and matched_ids:
            try:
                _prior_atom_state = self.store.get_memory(matched_ids[0])
            except Exception:
                pass
        # ── Step 4: Write action ──────────────────────────────────────────────
        from scripts.supersede_memory import set_lifecycle_status

        duplicate_ids: list[str] = []
        reinforces_ids: list[str] = []
        refines_ids: list[str] = []
        supersedes_ids: list[str] = []
        conflicts_ids: list[str] = []
        committed_atom_id: str | None = None
        committed_signal_id: str | None = None
        proposal_id: str | None = None

        if final_decision in ("commit", "refine_existing", "supersede_existing"):
            rel_for_store = (
                "refinement"
                if final_decision in ("refine_existing", "supersede_existing")
                else "new"
            )
            exact_match = self.store.find_exact_content_match(final_text)
            if exact_match:
                # Degrade to reinforce — already stored verbatim
                final_decision = "reinforce_existing"
                lifecycle_action = "signal_added"
                ex_id = str(exact_match["id"])
                duplicate_ids = [ex_id]
                matched_ids = [ex_id]
            else:
                # For model-scoped atoms, the caller supplies a precise injection
                # directive as context_summary. The critic rewrites it to a generic
                # description ("This observation is relevant for...") which is useless
                # for prompt injection. Prefer the candidate's provided context_summary
                # when the scope is model:* and a context_summary was explicitly supplied.
                incoming_context_summary = candidate.get("context_summary", "")
                is_model_scope = bool(scope and str(scope).startswith("model:"))
                if is_model_scope and incoming_context_summary and incoming_context_summary.strip():
                    effective_context_summary = incoming_context_summary.strip()
                else:
                    effective_context_summary = critic.get("context_summary") or final_text
                atom_id, signal_id = self.store.store_memory_with_signal(
                    content=final_text,
                    context_summary=effective_context_summary,
                    memory_type=final_type,
                    scope=final_scope,
                    confidence=max(0.0, min(1.0, final_conf)),
                    importance=importance,
                    relationship=rel_for_store,
                    reconciliation_reason=reconciler_reason,
                    signal_metadata={"matched_memory_ids": matched_ids} if matched_ids else None,
                    source_key=effective_source_key,
                    source_type=effective_source_type,
                    atom_source_type=effective_source_type,
                    source_url=(
                        effective_source_key
                        if effective_source_type == "web_research"
                        and effective_source_key.startswith("http")
                        else None
                    ),
                    source_user_id=source_user_id,
                    visibility=_effective_visibility(visibility, critic),
                )
                committed_atom_id = atom_id
                committed_signal_id = signal_id
                # Write novelty fields from critic output
                _novelty = float(critic.get("novelty_score") or 0.0)
                _interest = bool(critic.get("interest_flag") or (_novelty >= 0.75))
                if _novelty > 0 or _interest:
                    try:
                        import psycopg as _pg
                        _cfg = self.store._cfg if hasattr(self.store, "_cfg") else None
                        _db_url = (
                            _cfg.database_url
                            if _cfg
                            else __import__("os").environ.get("DATABASE_URL", "")
                        )
                        with _pg.connect(_db_url) as _conn:
                            with _conn.cursor() as _cur:
                                _cur.execute(
                                    "UPDATE memory_atoms SET novelty_score=%s, interest_flag=%s WHERE id=%s;",
                                    (_novelty, _interest, committed_atom_id),
                                )
                            _conn.commit()
                    except Exception:
                        pass  # non-fatal
                # Auto-link to highly similar neighbors (cosine > 0.85).
                # Builds the knowledge graph organically without manual calls.
                _AUTO_LINK_THRESHOLD = 0.85
                for neighbor in related_memories:
                    neighbor_sim = float(neighbor.get("similarity", 0.0))
                    neighbor_id = str(neighbor.get("id") or "")
                    if neighbor_id and neighbor_sim >= _AUTO_LINK_THRESHOLD:
                        try:
                            self.store.link_atoms(
                                atom_a_id=committed_atom_id,
                                atom_b_id=neighbor_id,
                                relation_type="related",
                                confidence=round(neighbor_sim, 3),
                            )
                        except Exception:
                            pass  # non-fatal
                if rel_for_store == "refinement" and matched_ids:
                    for old_id in matched_ids:
                        try:
                            sup = set_lifecycle_status(
                                old_id,
                                "superseded",
                                new_atom_id=committed_atom_id,
                                reason=(
                                    f"Superseded by {final_decision}: "
                                    f"{committed_atom_id}"
                                ),
                            )
                            if sup.get("updated"):
                                supersedes_ids.append(old_id)
                        except Exception:
                            pass
                    refines_ids = list(matched_ids)

        if final_decision == "reinforce_existing" and matched_ids:
            reinforces_ids = list(matched_ids)
            try:
                self.store.add_signal_to_atom(
                    atom_id=matched_ids[0],
                    content=final_text,
                    relationship="reinforcement",
                    memory_type=final_type,
                    scope=final_scope,
                    confidence=final_conf,
                    importance=importance,
                    reconciliation_reason=reconciler_reason,
                    source_key=effective_source_key,
                    source_type=effective_source_type,
                    source_user_id=source_user_id,
                )
            except Exception:
                pass  # non-fatal; trace still written

        elif final_decision in ("mark_conflict", "propose_for_review"):
            signal_rel = (
                reconciler_rel
                if reconciler_rel in ("conflict", "opinion_change")
                else ("conflict" if final_decision == "mark_conflict" else "refinement")
            )
            if matched_ids:
                # Route through signal math: add opposition signal to existing atom
                # and let recompute_atom_weights update disagreement_score + confidence.
                # This replaces the human-reviewed proposal queue entirely.
                try:
                    self.store.add_signal_to_atom(
                        atom_id=matched_ids[0],
                        content=final_text,
                        relationship=signal_rel,
                        context_summary=critic.get("context_summary") or final_text,
                        memory_type=final_type,
                        scope=final_scope,
                        confidence=max(0.0, min(1.0, final_conf)),
                        importance=importance,
                        reconciliation_reason=reconciler_reason,
                        source_key=effective_source_key,
                        source_type=effective_source_type,
                        source_user_id=source_user_id,
                    )
                    conflicts_ids = list(matched_ids)
                except Exception:
                    pass  # non-fatal; conflict recorded in trace
            else:
                # No existing atom to conflict with — commit as new
                atom_id, signal_id = self.store.store_memory_with_signal(
                    content=final_text,
                    context_summary=critic.get("context_summary") or final_text,
                    memory_type=final_type,
                    scope=final_scope,
                    confidence=max(0.0, min(1.0, final_conf)),
                    importance=importance,
                    relationship="new",
                    reconciliation_reason=reconciler_reason,
                    source_key=effective_source_key,
                    source_type=effective_source_type,
                    source_user_id=source_user_id,
                    visibility=visibility,
                )
                committed_atom_id = atom_id
                committed_signal_id = signal_id
        # ── Belief revision log ────────────────────────────────────────────────
        # Write an immutable revision entry for every event on a revisable type.
        # Failures are non-fatal — the commit itself is already written.
        if final_type in REVISABLE_TYPES:
            try:
                if final_decision == "commit" and committed_atom_id:
                    self.store.log_belief_revision(
                        atom_id=committed_atom_id,
                        new_content=final_text,
                        new_confidence=final_conf,
                        memory_type=final_type,
                        event_type="commit",
                        scope=final_scope,
                        source_key=effective_source_key,
                    )
                elif (
                    final_decision in ("refine_existing", "supersede_existing")
                    and committed_atom_id
                ):
                    event = (
                        "supersede"
                        if final_decision == "supersede_existing"
                        else "refine"
                    )
                    self.store.log_belief_revision(
                        atom_id=committed_atom_id,
                        new_content=final_text,
                        new_confidence=final_conf,
                        memory_type=final_type,
                        event_type=event,
                        scope=final_scope,
                        prior_atom_id=matched_ids[0] if matched_ids else None,
                        prior_content=(
                            _prior_atom_state.get("content")
                            if _prior_atom_state
                            else None
                        ),
                        prior_confidence=(
                            float(_prior_atom_state["confidence"])
                            if _prior_atom_state
                            and _prior_atom_state.get("confidence") is not None
                            else None
                        ),
                        revision_reason=reconciler_reason or None,
                        source_key=effective_source_key,
                    )
                elif (
                    final_decision == "reinforce_existing"
                    and reinforces_ids
                    and _prior_atom_state
                ):
                    self.store.log_belief_revision(
                        atom_id=reinforces_ids[0],
                        new_content=_prior_atom_state.get("content", final_text),
                        new_confidence=final_conf,
                        memory_type=final_type,
                        event_type="reinforce",
                        scope=final_scope,
                        prior_content=_prior_atom_state.get("content"),
                        prior_confidence=(
                            float(_prior_atom_state["confidence"])
                            if _prior_atom_state.get("confidence") is not None
                            else None
                        ),
                        revision_reason=reconciler_reason or None,
                        source_key=effective_source_key,
                    )
            except Exception:
                pass  # revision log failure is non-fatal
        # ── Step 5: Commit trace ──────────────────────────────────────────────
        decision_obj = CommitDecision(
            decision=final_decision,
            candidate_text=content,
            final_memory_text=final_text if final_decision != "reject" else None,
            scope=final_scope,
            memory_type=final_type,
            confidence=final_conf,
            lifecycle_action=lifecycle_action,
            duplicate_atom_ids=duplicate_ids,
            reinforces_atom_ids=reinforces_ids,
            refines_atom_ids=refines_ids,
            supersedes_atom_ids=supersedes_ids,
            conflicts_with_atom_ids=conflicts_ids,
            critic_notes=critic.get("critic_notes") or [],
            write_action=final_decision,
            committed_atom_id=committed_atom_id,
            committed_signal_id=committed_signal_id,
            proposal_id=proposal_id,
            rejection_reason=(
                critic.get("rejection_reason") if final_decision == "reject" else None
            ),
        )
        try:
            trace_id = self.store.store_commit_trace(decision_obj.to_dict())
            decision_obj.trace_id = trace_id
        except Exception:
            pass  # trace failure is non-fatal

        # Advance discussion thread_status → 'updated' after a successful dual-write.
        # Only escalates; never overwrites answered/validated/reopened.
        if final_decision == "commit" and committed_atom_id:
            try:
                import psycopg as _pg
                _db_url = (
                    getattr(self.store, "_dsn", None)
                    or __import__("os").environ.get("DATABASE_URL", "")
                )
                if _db_url:
                    with _pg.connect(_db_url) as _conn:
                        with _conn.cursor() as _cur:
                            _cur.execute(
                                """
                                UPDATE discussions d
                                SET thread_status = 'updated'
                                FROM discussion_atoms da
                                WHERE da.discussion_id = d.id
                                  AND da.atom_id = %s
                                  AND d.thread_status IN ('active', 'gathering');
                                """,
                                (committed_atom_id,),
                            )
                        _conn.commit()
            except Exception:
                pass  # discussion status advancement is non-fatal

        return decision_obj
