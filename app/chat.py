from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from app.config import get_config
from app.llm_provider import get_llm_client
from app.memory_store import MemoryStore

_logger = logging.getLogger(__name__)
# One background reflection job at a time — prevents LLM call stacking on rapid turns.
_reflection_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="reflection")


LABEL_PATTERN = re.compile(r"\[[^\[\]\n]*/[^\[\]\n]*/\d(?:\.\d+)?\]")
_THINK_PATTERN = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_MAX_EVAL_ITERATIONS = 2  # max revision passes per turn (up to _MAX_EVAL_ITERATIONS+1 LLM draft calls)
_MAX_GAP_SEARCHES = 5    # safety backstop — LLM self-regulates primary; this guards edge cases

# Memory routing: these atom types and conditions trigger the RECURSIVE path.
_TRIGGER_MEMORY_TYPES = frozenset({"correction", "warning"})
_ROUTE_MIN_SIMILARITY: float = 0.35   # below this → DIRECT (answer from training)
_ROUTE_HIGH_DISAGREEMENT: float = 0.4 # above this → RECURSIVE


@dataclass
class GapResolutionState:
    """Working memory shared across pre-draft gap planning and post-draft evaluation.

    Both phases read and write the same state so the post-draft evaluator never
    re-discovers what was already resolved pre-draft, and all web searches are
    deduplicated across the full turn.
    """
    original_message: str
    searched_queries: set[str] = field(default_factory=set)   # dedup across full turn
    accumulated_research: list[dict] = field(default_factory=list)  # all web hits this turn
    status: str = "in_progress"  # "in_progress" | "resolved" | "needs_human"
    clarifying_question: str | None = None  # populated only for needs_human

# Shared epistemic-status rules injected into every system prompt.
# Replaces the vague "do not claim unsupported facts" instruction with explicit
# definitions and labelling behaviour so the model labels instead of suppresses.
_EPISTEMIC_RULES = (
    "Epistemic-status rules:\n"
    "  Only add epistemic labels ('best guess', 'unverified', etc.) when the uncertainty"
    " is material — i.e. the user would make a different decision if they knew.\n"
    "  Do not label every claim. Most claims in normal conversation do not need a label.\n"
    "  When asked for an opinion, give it directly. Do not over-hedge.\n"
    "  If web research would genuinely help and is unavailable, say so once — briefly.\n"
)


def _extract_thinking(raw: str) -> tuple[str, str]:
    """Split raw LLM output into (answer_text, thinking_text).

    thinking_text concatenates the inner content of all <think>...</think> blocks.
    answer_text is raw with those blocks removed.
    Returns (raw, "") when no think blocks are present.
    """
    inner_parts = re.findall(r"<think>(.*?)</think>", raw, re.DOTALL | re.IGNORECASE)
    thinking_text = "\n".join(p.strip() for p in inner_parts if p.strip())
    answer_text = _THINK_PATTERN.sub("", raw).strip()
    return answer_text, thinking_text


def _retrieve_with_fallback(
    message: str,
    memory_store: MemoryStore,
    limit: int,
    scope_filter: str | None = None,
    scope_filters: list[str] | None = None,
) -> list[dict]:
    """Retrieve memories with progressive threshold relaxation.

    Falls back to lower similarity thresholds when no atoms are found at the
    primary threshold. Returns the first non-empty result, or [] if all
    thresholds are exhausted. The context evaluator still runs once on the result.
    """
    cfg = get_config()
    thresholds = (
        list(cfg.adaptive_retrieval_thresholds)
        if cfg.adaptive_retrieval_enabled
        else [cfg.memory_retrieval_threshold]
    )
    for threshold in thresholds:
        atoms = memory_store.retrieve_memories(
            message, limit=limit, min_similarity=threshold,
            scope_filter=scope_filter, scope_filters=scope_filters,
        )
        if atoms:
            return atoms
    return []


def _classify_retrieval_signal(atoms: list[dict]) -> str:
    """Classify retrieved atoms into a routing path. No LLM involved.

    Returns
    -------
    "direct"    — no atoms or all below similarity threshold → answer from training
    "context"   — relevant, clean atoms → inject and answer once
    "recursive" — correction/warning atom, contested lifecycle, or high disagreement
                  → draft, check against trigger atoms, optionally research, revise
    """
    if not atoms:
        return "direct"

    max_sim = max((float(a.get("similarity") or 0.0) for a in atoms), default=0.0)
    if max_sim < _ROUTE_MIN_SIMILARITY:
        return "direct"

    for atom in atoms:
        if str(atom.get("memory_type", "")).lower() in _TRIGGER_MEMORY_TYPES:
            return "recursive"
        if str(atom.get("lifecycle_status", "")).lower() == "contested":
            return "recursive"
        if float(atom.get("disagreement_score") or 0.0) > _ROUTE_HIGH_DISAGREEMENT:
            return "recursive"

    return "context"


def build_prompt(user_prompt: str, memories: list[dict]) -> tuple[str, str]:
    memory_lines = []
    for memory in memories:
        scope_value = memory.get("scope") or "global"
        similarity = memory.get("similarity", 0.0)
        injected_text = memory.get("context_summary") or memory.get("content")
        line = (
            f"- [{memory.get('memory_type')}/{scope_value}/{similarity:.3f}] "
            f"{injected_text}"
        )
        memory_lines.append(line)

    if not memory_lines:
        memory_lines.append("- (none)")

    joined_memory_lines = "\n".join(memory_lines)
    system = (
        "You are a helpful assistant with access to retrieved memory. Respond conversationally and directly — no stiff phrasing, no unnecessary hedging.\n"
        "Your training knowledge is always available as a baseline. Retrieved memory supplements it — it does not replace it.\n"
        "If retrieved memories are irrelevant to the question, ignore them and answer from your training.\n"
        "If the user gives a new instruction, correction, or preference, acknowledge it naturally.\n"
        "Do not say 'there is no retrieved memory for that' or expose bracket metadata tags.\n"
        + _EPISTEMIC_RULES
    )
    context = (
        "Retrieved memories:\n"
        + f"{joined_memory_lines}\n\n"
        + "User prompt:\n"
        + f"{user_prompt}\n"
    )
    return system, context


def chat_with_memory(user_prompt: str, limit: int = 5) -> tuple[str, list[dict]]:
    memory_store = MemoryStore()
    llm = get_llm_client()

    memories = memory_store.retrieve_memories(user_prompt, limit=limit)
    system, context = build_prompt(user_prompt, memories)
    answer = llm.generate_response(context, system=system)
    return answer, memories


def build_prompt_with_history(
    messages: list[dict],
    memories: list[dict],
    research_results: list[dict] | None = None,
    context_eval: "ContextEvaluation | None" = None,  # type: ignore[name-defined]  # noqa: F821
    revision_notes: str | None = None,
) -> tuple[str, str]:
    """Multi-turn prompt: retrieved memories + optional web research + conversation history.

    messages: list of {role: "user"|"assistant", content: str}
    Last entry must be the current user message.
    History is limited to the preceding 6 messages (3 turns) to cap prompt size.
    research_results: optional list of {title, url, snippet} from a web provider.
    context_eval: optional evaluation result — adjusts system prompt caveats.
    """
    memory_lines = []
    for memory in memories:
        scope_value = memory.get("scope") or "global"
        similarity = memory.get("similarity", 0.0)
        injected_text = memory.get("context_summary") or memory.get("content")
        line = (
            f"- [{memory.get('memory_type')}/{scope_value}/{similarity:.3f}] "
            f"{injected_text}"
        )
        memory_lines.append(line)

    if not memory_lines:
        memory_lines.append("- (none)")

    joined_memories = "\n".join(memory_lines)

    # Build optional research section
    research_section = ""
    if research_results:
        research_lines = []
        for r in research_results:
            title = r.get("title", "—")
            url = r.get("url", "")
            snippet = r.get("snippet", "")
            entry = f"- {title}: {snippet}"
            if url:
                entry += f" (source: {url})"
            research_lines.append(entry)
        research_section = (
            "Current web search results (retrieved for this query — use these to answer directly):\n"
            + "\n".join(research_lines)
            + "\n\n"
        )

    prior = messages[:-1][-get_config().history_window_size:]  # window from config (default 6)
    history_section = ""
    if prior:
        lines = []
        for msg in prior:
            prefix = "User" if msg["role"] == "user" else "Assistant"
            lines.append(f"{prefix}: {msg['content']}")
        history_section = "Recent conversation:\n" + "\n".join(lines) + "\n\n"

    current_message = messages[-1]["content"]

    # Context-evaluation caveat injection
    eval_instruction = ""
    if context_eval:
        action = context_eval.final_action
        status = context_eval.context_status
        if action == "answer_with_caveat" or status in ("conflicting", "stale"):
            eval_instruction = (
                "Memory context quality note: some retrieved memories have conflicting signals "
                "or staleness concerns. Apply epistemic labels only where it materially affects "
                "the answer's reliability.\n"
            )
        elif action == "verify_then_answer":
            eval_instruction = (
                "Memory context quality note: retrieved context may require verification. "
                "Label claims that depend on potentially unverified or stale memory as 'unverified' or 'best guess'.\n"
            )
        elif status in ("insufficient", "unsupported") or action == "ask_user":
            eval_instruction = (
                "Retrieved memory is not relevant to this question. "
                "Answer from your own training knowledge directly — do not mention the memory gap, "
                "do not ask for clarification unless the question itself is genuinely unclear.\n"
            )

    system = (
        "You are a helpful assistant with access to retrieved memory and conversation history. Respond conversationally and directly — no stiff phrasing, no unnecessary hedging.\n"
        "Your training knowledge is always available as a baseline. Retrieved memory supplements it — it does not replace it.\n"
        "If retrieved memories are irrelevant to the question, ignore them and answer from your training.\n"
        "If the user gives a new instruction, correction, or preference, acknowledge it naturally.\n"
        "Do not say 'there is no retrieved memory for that' or expose bracket metadata tags.\n"
        + _EPISTEMIC_RULES
        + eval_instruction
        + (
            "Web research results are included below. Base your answer on them directly."
            " Do not claim you lack access to current information — those results ARE your current information.\n"
            if research_results
            else
            "No web search results were retrieved for this query. If the user expects a web"
            " lookup or URL fetch, say so honestly — do not invent an explanation about training"
            " data cutoffs or claim capabilities you are not exercising right now.\n"
        )
        + (f"\n{revision_notes}\n" if revision_notes else "")
    )
    context = (
        f"Retrieved memories:\n{joined_memories}\n\n"
        f"{research_section}"
        f"{history_section}"
        f"User: {current_message}\n"
    )
    return system, context


# ── Iterative evaluation helpers ──────────────────────────────────────────────

def _build_revision_notes(response_eval: "ResponseEvaluation") -> str:  # type: ignore[name-defined]  # noqa: F821
    """Summarise evaluation issues as explicit revision instructions for the next draft."""
    lines = ["[Evaluation feedback — address in this response:]"]  
    for issue in response_eval.issues[:5]:
        itype = issue.get("type", "issue")
        sev = issue.get("severity", "medium")
        detail = issue.get("detail", "")
        lines.append(f"  \u2022 [{sev}] {itype}: {detail}")
    if response_eval.overstatement_risk not in (None, "none"):
        lines.append(
            f"  \u2022 Overstatement risk '{response_eval.overstatement_risk}': "
            "lower confidence on claims not directly supported by retrieved memory."
        )
    if response_eval.reasoning:
        lines.append(f"Evaluator: {response_eval.reasoning}")
    lines.append(
        "Revise to address the above. Label uncertain claims with "
        "'best guess', 'unverified', or 'weakly supported'."
    )
    return "\n".join(lines)


def _gather_research_evidence(
    issues: list[dict],
    fallback_query: str,
    provider: "BaseResearchProvider",  # type: ignore[name-defined]  # noqa: F821
    searched_queries: set[str] | None = None,
) -> list[dict]:
    """Mid-loop web search for claims the evaluator flagged as unverified or invented.

    Uses issue 'detail' fields as targeted queries so the search is specific to the
    gap, not just the original user message.  Sensitive queries are silently skipped.
    Deduplicates against searched_queries when provided (shared with pre-draft phase).
    Returns [] when the provider is unavailable or all searches fail.
    """
    from app.research import _is_sensitive

    queries: list[str] = []
    for issue in issues:
        if issue.get("type") in ("unverified_claim", "invented_fact"):
            detail = issue.get("detail", "").strip()
            if detail and not _is_sensitive(detail):
                queries.append(detail)
    queries = queries[:2] or (
        [] if _is_sensitive(fallback_query) else [fallback_query]
    )
    if not queries:
        return []

    seen_urls: set[str] = set()
    results: list[dict] = []
    for q in queries:
        if searched_queries is not None and q in searched_queries:
            continue
        try:
            for hit in provider.search(q, limit=3):
                url = hit.get("url", "")
                if url not in seen_urls:
                    seen_urls.add(url)
                    results.append(hit)
            if searched_queries is not None:
                searched_queries.add(q)
        except Exception:
            pass
    return results


def _gather_verification_evidence(
    issues: list[dict],
    fallback_query: str,
    memory_store: "MemoryStore",
    scope_filter: str | None = None,
) -> list[dict]:
    """Re-retrieve targeted atoms for flagged unverified or invented claims."""
    queries = [
        issue.get("detail", "")
        for issue in issues
        if issue.get("type") in ("unverified_claim", "invented_fact", "overstatement")
    ]
    queries = [q.strip() for q in queries if q.strip()][:3] or [fallback_query]
    seen: set[str] = set()
    results: list[dict] = []
    for q in queries:
        try:
            hits = memory_store.retrieve_memories(q, limit=3, min_similarity=0.40, scope_filter=scope_filter)
            for h in hits:
                hid = str(h.get("id", ""))
                if hid and hid not in seen:
                    seen.add(hid)
                    results.append(h)
        except Exception:
            pass
    return results


def _iterative_evaluation_loop(
    messages: list[dict],
    used_memories: list[dict],
    research_results: list[dict],
    context_eval: "ContextEvaluation | None",  # type: ignore[name-defined]  # noqa: F821
    memory_store: "MemoryStore",
    llm: "LLMProvider",  # type: ignore[name-defined]  # noqa: F821
    research_provider: "BaseResearchProvider | None" = None,  # type: ignore[name-defined]  # noqa: F821
    gap_state: GapResolutionState | None = None,
) -> "tuple[str, str, ResponseEvaluation | None]":  # type: ignore[name-defined]  # noqa: F821
    """Draft → evaluate → revise loop.

    Accepts an optional GapResolutionState shared with the pre-draft gap resolution
    phase.  When provided, augmented_research is seeded from state.accumulated_research
    and all mid-loop web searches are deduplicated via state.searched_queries — so the
    post-draft evaluator never re-fetches what was already resolved pre-draft.

    When the evaluator flags unverified_claim or invented_fact and a research_provider
    is available, the loop triggers targeted mid-loop searches using the gap state's
    dedup set.  Stops early on 'approved'/'needs_caveat' verdict.
    Returns (final_answer, thinking, last_response_eval).
    """
    from app.response_evaluator import ResponseEvaluator

    current_message = messages[-1]["content"]
    re_evaluator = ResponseEvaluator(llm=llm)
    augmented_memories = list(used_memories)

    # Seed research from shared gap state if available, otherwise from caller
    if gap_state is not None:
        augmented_research: list[dict] = gap_state.accumulated_research
    else:
        augmented_research = list(research_results)

    # If pre-draft phase determined needs_human, note it but do NOT force a clarifying
    # question — the model should answer from training knowledge first, and only ask
    # for clarification if it genuinely cannot answer at all.
    initial_revision: str | None = None
    if gap_state is not None and gap_state.status == "needs_human" and gap_state.clarifying_question:
        initial_revision = (
            "[Note: no external sources found for this topic. Answer from your own training "
            "knowledge. If the question is genuinely unanswerable without private user input, "
            f"you may ask: {gap_state.clarifying_question} — but only if truly necessary.]"
        )

    revision_notes: str | None = initial_revision
    final_answer = ""
    thinking = ""
    response_eval = None

    for iteration in range(_MAX_EVAL_ITERATIONS + 1):
        system, context = build_prompt_with_history(
            messages,
            augmented_memories,
            research_results=augmented_research or None,
            context_eval=context_eval,
            revision_notes=revision_notes,
        )
        draft = llm.generate_response(context, system=system)
        draft, thinking = _extract_thinking(draft)
        final_answer = draft

        try:
            response_eval = re_evaluator.evaluate(
                draft, context_eval, augmented_memories, current_message
            )
            if response_eval.revised_answer:
                final_answer = response_eval.revised_answer
        except Exception as exc:
            _logger.warning(
                "iterative_eval: ResponseEvaluator failed on iteration %d: %s", iteration, exc
            )
            break

        if response_eval.verdict in ("approved", "needs_caveat"):
            break

        if iteration >= _MAX_EVAL_ITERATIONS:
            _logger.debug(
                "iterative_eval: max iterations (%d) reached; verdict=%s",
                _MAX_EVAL_ITERATIONS,
                response_eval.verdict,
            )
            break

        _logger.debug(
            "iterative_eval: iteration %d verdict=%s; re-drafting with enriched context",
            iteration,
            response_eval.verdict,
        )

        # 1. Re-retrieve targeted memory atoms for flagged claims.
        if response_eval.verdict == "needs_verification":
            additional = _gather_verification_evidence(
                response_eval.issues, current_message, memory_store,
                scope_filter=None,
            )
            seen = {str(m.get("id", "")) for m in augmented_memories}
            augmented_memories = augmented_memories + [
                m for m in additional if str(m.get("id", "")) not in seen
            ]

        # 2. If memory doesn't cover the gap, trigger a targeted web search.
        #    Dedup via gap_state.searched_queries (shared with pre-draft phase).
        has_knowledge_gap = any(
            issue.get("type") in ("unverified_claim", "invented_fact")
            for issue in response_eval.issues
        )
        if has_knowledge_gap and research_provider is not None and research_provider.is_available():
            searched = gap_state.searched_queries if gap_state is not None else set()
            if len(searched) < _MAX_GAP_SEARCHES:
                web_hits = _gather_research_evidence(
                    response_eval.issues, current_message, research_provider,
                    searched_queries=searched,
                )
                if web_hits:
                    augmented_research = augmented_research + web_hits
                    if gap_state is not None:
                        gap_state.accumulated_research.extend(web_hits)
                    _reflection_pool.submit(_store_research_bg, web_hits, current_message)
                    _logger.debug(
                        "iterative_eval: mid-loop web search returned %d result(s) at iteration %d",
                        len(web_hits), iteration,
                    )

        revision_notes = _build_revision_notes(response_eval)

    return final_answer, thinking, response_eval


def chat_with_history(messages: list[dict], limit: int = 5) -> tuple[str, list[dict]]:
    """Multi-turn chat with memory retrieval.

    messages: list of {role: "user"|"assistant", content: str}
    The last entry must have role="user".
    Returns: (answer, retrieved_memories)
    """
    from app.context_evaluator import RuntimeContextEvaluator

    current_message = messages[-1]["content"]
    memory_store = MemoryStore()
    llm = get_llm_client()

    memories = _retrieve_with_fallback(
        current_message, memory_store, limit=limit,
        scope_filters=["project:memory-layer", "user"],
    )

    evaluator = RuntimeContextEvaluator(store=memory_store, llm=llm)
    context_eval = evaluator.evaluate(current_message, memories)
    try:
        trace_id = memory_store.store_context_trace(context_eval.to_dict())
        context_eval.trace_id = trace_id
    except Exception:
        pass

    used_ids = set(context_eval.used_atom_ids)
    used_memories = [m for m in memories if m.get("id") in used_ids] or memories

    # Iterative evaluation loop: draft → evaluate → revise (up to _MAX_EVAL_ITERATIONS passes)
    final_answer, thinking, response_eval = _iterative_evaluation_loop(
        messages, used_memories, [], context_eval, memory_store, llm
    )

    if response_eval is not None:
        try:
            rt = response_eval.to_dict()
            rt["user_message"] = current_message
            rt["draft_answer"] = final_answer
            rt["final_answer"] = final_answer
            rt["context_trace_id"] = context_eval.trace_id if context_eval else None
            rt["gap_status"] = "resolved"  # no gap resolution on this path
            rt["gap_searches"] = 0
            response_eval.trace_id = memory_store.store_response_trace(rt)
        except Exception:
            pass

    _reflection_pool.submit(_post_turn_reflection, current_message, thinking, final_answer, llm, memory_store)

    return final_answer, memories


def chat_with_research(
    messages: list[dict],
    limit: int = 5,
) -> tuple[str, list[dict], list[dict], str, "ContextEvaluation | None", GapResolutionState, str]:  # type: ignore[name-defined]  # noqa: F821
    """Multi-turn chat routed by memory retrieval signal.

    Routes each turn through one of three paths:
    - DIRECT   — no relevant atoms → answer from training, single LLM call
    - CONTEXT  — clean relevant atoms → inject memory and answer, single LLM call
    - RECURSIVE — correction/warning/contested atom → draft, check, research, revise

    messages: list of {role: "user"|"assistant", content: str}
    The last entry must have role="user".

    Returns: (answer, retrieved_memories, all_web_results, research_status, context_eval, gap_state, route)

    research_status values
    ----------------------
    "used"                     — web results found and injected into the prompt
    "no_results"               — research triggered but provider returned nothing
    "provider_unavailable"     — research needed but no provider configured
    "sensitive_content"        — message blocked from external search
    "user_opted_out"           — user explicitly declined web search this turn
    "local_context_sufficient" — direct/context path; no research needed
    """
    from app.research import get_research_provider, should_use_research, fetch_urls_in_message
    from app.context_evaluator import RuntimeContextEvaluator, ContextEvaluation

    current_message = messages[-1]["content"]
    memory_store = MemoryStore()
    llm = get_llm_client()

    atoms = _retrieve_with_fallback(
        current_message, memory_store, limit=limit,
        scope_filters=["project:memory-layer", "user"],
    )

    route = _classify_retrieval_signal(atoms)

    # Deterministic context eval — no LLM critic — used for traces and caveat injection.
    evaluator = RuntimeContextEvaluator(store=memory_store, llm=llm)
    context_eval: ContextEvaluation | None = None
    try:
        context_eval = evaluator.evaluate(current_message, atoms, use_critic=False)
        trace_id = memory_store.store_context_trace(context_eval.to_dict())
        context_eval.trace_id = trace_id
    except Exception:
        pass

    if context_eval and context_eval.used_atom_ids:
        used_ids = set(context_eval.used_atom_ids)
        used_memories = [m for m in atoms if m.get("id") in used_ids]
    else:
        used_memories = atoms

    gap_state = GapResolutionState(original_message=current_message)
    gap_state.status = "resolved"
    final_answer = ""
    thinking = ""
    response_eval = None

    # ── Pre-routing research: fires on ALL routes ──────────────────────────────
    # 1. Always fetch any URLs the user included in their message (no search provider needed).
    url_results = fetch_urls_in_message(current_message)

    # 2. Keyword search via configured provider.
    provider = get_research_provider()
    use_kw_search, kw_reason = should_use_research(
        current_message, used_memories, provider, llm=llm
    )
    kw_results: list[dict] = []
    if use_kw_search:
        try:
            kw_results = provider.search(current_message, limit=3)
        except Exception:
            kw_results = []

    early_research = url_results + kw_results
    if early_research:
        research_status = "used"
        _reflection_pool.submit(_store_research_bg, early_research, current_message)
    elif use_kw_search and not kw_results:
        research_status = "no_results"
    else:
        research_status = kw_reason

    gap_state.accumulated_research = early_research
    # ──────────────────────────────────────────────────────────────────────────

    if route == "direct":
        # No relevant memory — answer from training (+ any web/URL results).
        system, context = build_prompt_with_history(
            messages, [], research_results=early_research or None
        )
        raw = llm.generate_response(context, system=system)
        final_answer, thinking = _extract_thinking(raw)

    elif route == "context":
        # Relevant clean atoms — inject memory (+ any web/URL results) and answer.
        system, context = build_prompt_with_history(
            messages, used_memories, research_results=early_research or None,
            context_eval=context_eval
        )
        raw = llm.generate_response(context, system=system)
        final_answer, thinking = _extract_thinking(raw)

    else:  # recursive
        # Memory flags a correction, warning, or contested belief.
        # Early research is already in gap_state; loop may add more via mid-loop searches.
        final_answer, thinking, response_eval = _iterative_evaluation_loop(
            messages, used_memories, [], context_eval, memory_store, llm,
            research_provider=provider,
            gap_state=gap_state,
        )

    # Store response trace.
    if response_eval is not None:
        try:
            rt = response_eval.to_dict()
            rt["user_message"] = current_message
            rt["draft_answer"] = final_answer
            rt["final_answer"] = final_answer
            rt["context_trace_id"] = context_eval.trace_id if context_eval else None
            rt["gap_status"] = gap_state.status
            rt["gap_searches"] = len(gap_state.searched_queries)
            rt["gap_clarifying_question"] = gap_state.clarifying_question
            response_eval.trace_id = memory_store.store_response_trace(rt)
        except Exception:
            pass
    else:
        try:
            memory_store.store_response_trace({
                "user_message": current_message,
                "draft_answer": final_answer,
                "final_answer": final_answer,
                "verdict": "approved",
                "context_trace_id": context_eval.trace_id if context_eval else None,
                "gap_status": gap_state.status,
                "gap_searches": 0,
            })
        except Exception:
            pass

    _reflection_pool.submit(
        _post_turn_reflection, current_message, thinking, final_answer, llm, memory_store
    )

    return final_answer, atoms, gap_state.accumulated_research, research_status, context_eval, gap_state, route


def _post_turn_reflection(
    user_msg: str,
    thinking: str,
    answer: str,
    llm,
    memory_store: MemoryStore,
) -> None:
    """Post-turn reflection: extract and commit durable insights from a turn.

    Delegates to app.reflection.run_turn_reflection — single source of truth
    shared with the MCP reflect_turn tool.  Runs in a background thread.
    """
    from app.reflection import run_turn_reflection
    run_turn_reflection(user_msg, thinking, answer)


def _store_research_bg(hits: list[dict], topic: str) -> None:
    """Background task: commit web research hits through the memory pipeline."""
    try:
        from app.research import store_research_findings
        store_research_findings(hits, topic=topic)
    except Exception as exc:
        _logger.warning("store_research_bg: failed: %s", exc)


def clean_assistant_response(answer: str) -> str:
    cleaned = _THINK_PATTERN.sub("", answer).strip()
    cleaned = LABEL_PATTERN.sub("", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    return cleaned


def _decision_to_event(decision: "CommitDecision") -> dict | None:  # type: ignore[name-defined]  # noqa: F821
    """Convert a CommitDecision from the pipeline into a chat event dict."""
    d = decision.decision
    if d in ("commit", "refine_existing", "supersede_existing"):
        return {
            "event": "stored",
            "memory_atom_id": decision.committed_atom_id,
            "memory_signal_id": decision.committed_signal_id,
            "content": decision.final_memory_text or decision.candidate_text,
            "memory_type": decision.memory_type,
            "scope": decision.scope,
            "relationship": d,
            "superseded_ids": decision.supersedes_atom_ids,
        }
    if d in ("mark_conflict", "propose_for_review"):
        return {
            "event": "proposed",
            "proposal_id": decision.proposal_id,
            "content": decision.final_memory_text or decision.candidate_text,
            "memory_type": decision.memory_type,
            "scope": decision.scope,
            "relationship": d,
        }
    if d == "reinforce_existing":
        return {
            "event": "reinforced",
            "atom_id": (
                decision.reinforces_atom_ids[0]
                if decision.reinforces_atom_ids
                else None
            ),
            "content": decision.candidate_text,
            "memory_type": decision.memory_type,
            "scope": decision.scope,
        }
    if d == "reject":
        return {
            "event": "skipped",
            "reason": decision.rejection_reason or "rejected",
            "content": decision.candidate_text,
        }
    return None


def extract_and_store_from_message(user_message: str) -> list[dict]:
    """Extract memory candidates from a user message and run the commit pipeline.

    Each candidate passes through: extraction → reconciliation → critic review
    → risk gate → write. All decisions are traced in memory_commit_traces.

    Each returned event dict has one of these shapes::

        {"event": "stored",     "memory_atom_id": str, "memory_signal_id": str,
         "content": str, "memory_type": str, "scope": str|None,
         "relationship": str, "superseded_ids": list[str]}

        {"event": "proposed",   "proposal_id": str, "content": str,
         "memory_type": str, "scope": str|None, "relationship": str}

        {"event": "reinforced", "atom_id": str|None, "content": str,
         "memory_type": str, "scope": str|None}

        {"event": "skipped",    "reason": str, "content": str}
    """
    from scripts.extract_memory_candidates import extract_candidates
    from app.commit_pipeline import MemoryCommitPipeline, CommitDecision

    try:
        extracted = extract_candidates(user_message, include_rejected=False)
    except Exception:
        return []

    candidates = [
        c for c in extracted.get("candidates", [])
        if isinstance(c, dict) and c.get("should_store")
    ]
    if not candidates:
        return []

    pipeline = MemoryCommitPipeline()
    events: list[dict] = []

    for candidate in candidates:
        try:
            decision: CommitDecision = pipeline.commit_candidate(candidate)
            event = _decision_to_event(decision)
            if event:
                events.append(event)
        except Exception:
            continue

    return events
