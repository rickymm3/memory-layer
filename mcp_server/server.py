"""stdio MCP server for memory-layer.

Exposes two read-only tools to VS Code / GitHub Copilot:
  - memory_health  : check database and Ollama reachability
  - memory_search  : semantic similarity search over memory_atoms

Run via:
  python -m mcp_server.server        (direct)
  make mcp                           (Makefile shortcut)
  .vscode/mcp.json                   (VS Code launches automatically)

All logs go to stderr. stdout is reserved for the MCP stdio protocol.
"""
from __future__ import annotations

import sys
from typing import Any

from mcp.server.fastmcp import FastMCP

from mcp_server.tools.extract import extract_memory_candidates
from mcp_server.tools.get import get_memory_by_id
from mcp_server.tools.get_signals import get_atom_signals
from mcp_server.tools.health import get_memory_health
from mcp_server.tools.kickoff import kickoff_project
from mcp_server.tools.project_context import get_project_context
from mcp_server.tools.propose_signal import propose_memory_signal
from mcp_server.tools.list_task_runs import list_task_runs
from mcp_server.tools.recent import get_recent_memories
from mcp_server.tools.task_context import get_task_context
from mcp_server.tools.assess_readiness import assess_task_readiness_handler
from mcp_server.tools.reconcile import reconcile_candidate
from mcp_server.tools.search import search_memories
from mcp_server.tools.recompute import recompute_memory_atom
from mcp_server.tools.store_approved import store_memory_approved
from mcp_server.tools.store_auto import store_memory_auto
from mcp_server.tools.reflect_turn import reflect_turn as _reflect_turn_impl
from mcp_server.tools.ingest_transcript import ingest_conversation_transcript as _ingest_impl
from mcp_server.tools.get_belief import get_belief_detail as _get_belief_impl
from mcp_server.tools.log_turn import log_turn as _log_turn_impl

mcp = FastMCP("memoryLayer")


@mcp.tool()
def memory_health() -> dict[str, Any]:
    """Check memory-layer health and return basic statistics.

    Returns reachability of the database and Ollama service, total atom count,
    and the configured embedding model name. Never exposes secrets, connection
    strings, file paths, or stack traces.
    """
    return get_memory_health()


@mcp.tool()
def memory_search(
    query: str,
    limit: int = 5,
    scope: str | None = None,
    memory_type: str | None = None,
    min_similarity: float = 0.0,
) -> list[dict[str, Any]]:
    """Search memory atoms by semantic similarity.

    Embeds the query and finds the most similar stored memory atoms using cosine
    similarity over pgvector. Use this to retrieve project constraints,
    decisions, domain facts, and lessons while coding.

    Args:
        query: Natural language question or topic to search for.
        limit: Maximum results to return. Clamped to 1–20. Default 5.
        scope: Optional scope filter (e.g. 'project', 'global').
        memory_type: Optional type filter (e.g. 'fact', 'decision', 'constraint').
        min_similarity: Minimum cosine similarity threshold (0.0–1.0). Exclude
            results below this score. Default 0.0 (no filtering). Recommended
            starting value: 0.45 to filter low-confidence tail results.
    """
    return search_memories(
        query=query,
        limit=limit,
        scope=scope,
        memory_type=memory_type,
        min_similarity=min_similarity,
    )


@mcp.tool()
def memory_project_context(
    scope: str,
    limit: int = 10,
    min_importance: float = 0.6,
    min_confidence: float = 0.7,
) -> list[dict[str, Any]]:
    """Return a curated snapshot of high-importance, high-confidence memory atoms.

    Use this to orient Copilot to a project's stored constraints, decisions, and
    facts before a coding session begins. Queries memory_atoms only; no writes.

    Args:
        scope: Required. Scope to filter by (e.g. 'project:memory-layer').
        limit: Maximum results to return. Clamped to 1–30. Default 10.
        min_importance: Minimum importance threshold (0.0–1.0). Default 0.6.
        min_confidence: Minimum confidence threshold (0.0–1.0). Default 0.7.
    """
    return get_project_context(
        scope=scope,
        limit=limit,
        min_importance=min_importance,
        min_confidence=min_confidence,
    )


@mcp.tool()
def memory_store_auto(
    content: str,
    memory_type: str,
    relationship: str,
    context_summary: str | None = None,
    scope: str | None = None,
    confidence: float = 0.8,
    importance: float = 0.5,
    reconciliation_reason: str | None = None,
    matched_memory_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Store a low-risk candidate via the write-policy auto-store path.

    Only call this when memory_reconcile_candidate returned is_auto_storable=true
    (relationship is 'new' or 'refinement' and recommended_action is 'store_new').
    Rejects conflict and opinion_change — use memory_propose_signal for those.

    Runs an exact-match guard before writing. Writes memory_atom + linked
    memory_signal in a single transaction. Returns a canonical write report;
    never stores silently.

    Args:
        content: Full canonical sentence from the extraction result.
        memory_type: Memory type from the extraction result.
        relationship: Must be 'new' or 'refinement'.
        context_summary: Compact summary from extraction. Defaults to content.
        scope: Scope string from the extraction result.
        confidence: Float 0.0–1.0 from extraction. Default 0.8.
        importance: Float 0.0–1.0 from extraction. Default 0.5.
        reconciliation_reason: Reconciler reason string.
        matched_memory_ids: Related atom UUIDs from reconciliation.
    """
    return store_memory_auto(
        content=content,
        memory_type=memory_type,
        relationship=relationship,
        context_summary=context_summary,
        scope=scope,
        confidence=confidence,
        importance=importance,
        reconciliation_reason=reconciliation_reason,
        matched_memory_ids=matched_memory_ids,
    )


@mcp.tool()
def memory_extract_candidates(
    text: str,
    context: str | None = None,
) -> list[dict[str, Any]]:
    """Extract memory candidate atoms from a text snippet without storing anything.

    Use this as the first step in the MCP write pipeline. Pass user-authored
    text; the extractor will identify facts, decisions, instructions, opinions,
    and preferences worth storing. Returns candidates ready to pass to
    memory_reconcile_candidate.

    Args:
        text: User-authored text to extract from. Do not pass assistant output
              as the primary text — it is context only.
        context: Optional surrounding context (assistant reply or retrieved
                 memories). Appended as context-only, not extracted from.
    """
    return extract_memory_candidates(text=text, context=context)


@mcp.tool()
def memory_reconcile_candidate(
    content: str,
    memory_type: str = "fact",
    scope: str | None = None,
    retrieve_limit: int = 5,
) -> dict[str, Any]:
    """Run the reconciler against existing atoms for a single candidate.

    Call this for each candidate returned by memory_extract_candidates before
    deciding whether to store it. Returns the relationship classification and
    whether the candidate is safe to auto-store.

    Check `is_auto_storable` in the response:
    - true  → call memory_store_auto
    - false → call memory_propose_signal (confirmation required)

    Args:
        content: Full candidate sentence to reconcile.
        memory_type: Memory type from the extraction result. Default fact.
        scope: Scope string from the extraction result.
        retrieve_limit: Related atoms to compare against. Clamped 1–10. Default 5.
    """
    return reconcile_candidate(
        content=content,
        memory_type=memory_type,
        scope=scope,
        retrieve_limit=retrieve_limit,
    )


@mcp.tool()
def memory_get(memory_id: str) -> dict[str, Any] | None:
    """Fetch a single memory atom by its UUID.

    Returns the full atom record, or null if the id does not exist. Use this
    to inspect a specific memory after finding its id via memory_search or
    memory_recent.

    Prefer `context_summary` for prompt injection and display; use `content`
    only when exact canonical wording matters.

    Args:
        memory_id: UUID string of the memory atom to fetch.
    """
    return get_memory_by_id(memory_id)


@mcp.tool()
def memory_recent(
    limit: int = 10,
    scope: str | None = None,
) -> list[dict[str, Any]]:
    """Return the most recently stored memory atoms, newest first.

    Use this to browse what the user has stored lately, orient to a project
    at session start without a specific query, or check what was written in
    the last session. Queries memory_atoms only; no writes.

    Args:
        limit: Maximum results to return. Clamped to 1–50. Default 10.
        scope: Optional scope filter (e.g. 'project:memory-layer').
               Omit to return atoms across all scopes.
    """
    return get_recent_memories(limit=limit, scope=scope)


@mcp.tool()
def memory_list_task_runs(
    scope: str | None = None,
    outcome: str | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Return recent task_run records, newest first.

    Use this to review prior task outcomes for a project before starting new
    work. Shows what tasks have been completed, which model was used, and how
    many lessons were stored. Queries task_runs only; no writes.

    Args:
        scope: Optional scope filter (e.g. 'project:memory-layer').
               Omit to return runs across all scopes.
        outcome: Optional outcome filter: 'success', 'partial', or 'failed'.
                 Omit to return all outcomes.
        limit: Maximum results to return. Clamped to 1–50. Default 10.
    """
    return list_task_runs(scope=scope, outcome=outcome, limit=limit)


@mcp.tool()
def memory_task_context(
    project_scope: str,
    model_scope: str | None = None,
    task_hint: str | None = None,
    recent_tasks: int = 5,
) -> dict[str, Any]:
    """Return a complete task-orientation snapshot before starting work.

    Combines project constraints, model-specific lessons, and recent task
    history in a single call. Use this instead of calling memory_project_context
    + memory_recent + memory_list_task_runs separately.

    Returns four sections:
    - project_context: high-importance project atoms (constraints, decisions)
    - model_lessons: all active atoms for the model (weaknesses, adaptations)
    - recent_task_runs: last N task_run records for this project
    - task_relevant_atoms: top semantic matches for task_hint (if provided)

    Plus a summary dict with counts and outcome distribution.

    Args:
        project_scope: Required. Project scope (e.g. 'project:memory-layer').
        model_scope: Optional. Model scope (e.g. 'model:qwen3-8b'). Returns
            known weaknesses and prompt adaptations for this model.
        task_hint: Optional. Short description of the task about to begin.
            Triggers a semantic search and adds task_relevant_atoms (~500 ms).
        recent_tasks: Number of recent task_runs to return. Clamped 1–20.
            Default 5.
    """
    return get_task_context(
        project_scope=project_scope,
        model_scope=model_scope,
        task_hint=task_hint,
        recent_tasks=recent_tasks,
    )


@mcp.tool()
def memory_propose_signal(
    content: str,
    memory_type: str,
    relationship: str,
    context_summary: str | None = None,
    scope: str | None = None,
    confidence: float = 0.8,
    importance: float = 0.5,
    reconciliation_reason: str | None = None,
    matched_memory_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Queue a confirmation-path candidate for human review.

    Call this when memory_reconcile_candidate returned is_auto_storable=false —
    typically when relationship is 'conflict', 'opinion_change', or 'ask_user'.
    The candidate is saved to memory_proposals with status='pending_review'.
    Nothing is written to memory_atoms or memory_signals yet.

    After queuing, the user runs `make review-proposals` in a terminal. The CLI
    shows the pending proposals, prompts for approve/reject, and on approval
    issues a short-lived single-use token. The user then calls
    memory_store_approved with the proposal_id and that token.

    Args:
        content: Full canonical sentence from the extraction result.
        memory_type: Memory type from the extraction result.
        relationship: Reconciler relationship. Typically 'conflict',
            'opinion_change', or 'ask_user'.
        context_summary: Compact summary. Defaults to content.
        scope: Scope string from the extraction result.
        confidence: Float 0.0–1.0. Default 0.8.
        importance: Float 0.0–1.0. Default 0.5.
        reconciliation_reason: Reconciler reason string.
        matched_memory_ids: Related atom UUIDs from reconciliation.

    Returns:
        {"proposal_id": "<uuid>", "status": "pending_review"}
        or {"error": "<message>"} on failure.
    """
    return propose_memory_signal(
        content=content,
        memory_type=memory_type,
        relationship=relationship,
        context_summary=context_summary,
        scope=scope,
        confidence=confidence,
        importance=importance,
        reconciliation_reason=reconciliation_reason,
        matched_memory_ids=matched_memory_ids,
    )


@mcp.tool()
def memory_store_approved(
    proposal_id: str,
    approval_token: str,
) -> dict[str, Any]:
    """Store a confirmation-path candidate after CLI review and approval.

    Call this only after the user has run `make review-proposals`, approved
    the proposal, and provided the printed approval token. The token is
    single-use and expires 5 minutes after issuance.

    Validates: proposal exists, status is 'approved', token matches, token
    not expired. On success stores memory_atom + memory_signal and marks the
    proposal as 'used'. Returns a canonical write report.

    Copilot cannot generate valid tokens. The token must come from the
    CLI review step — this is intentional to enforce human review for any
    write that was not auto-approved.

    Args:
        proposal_id: UUID returned by memory_propose_signal.
        approval_token: Short-lived token printed by `make review-proposals`.

    Returns:
        Canonical write report with auto_stored=false on success.
        {"stored": false, "error": "<message>"} on any validation failure.
    """
    return store_memory_approved(
        proposal_id=proposal_id,
        approval_token=approval_token,
    )


@mcp.tool()
def memory_recompute_atom(atom_id: str) -> dict[str, Any]:
    """Recompute signal-aggregation weights for a single memory atom.

    Fetches all linked memory_signals for the atom, recomputes support_weight,
    opposition_weight, disagreement_score, and confidence from the signal
    history, and persists the results to memory_atoms.

    Call this after CLI-path writes (memory_store_approved) to refresh the
    atom's aggregation fields, or after `make recompute-weights` to bulk-refresh.
    Auto-store writes (memory_store_auto) trigger recomputation automatically.

    Args:
        atom_id: UUID of the memory atom to recompute.

    Returns:
        Updated atom dict including all aggregation fields on success.
        {"error": "not found", "atom_id": "<uuid>"} if the atom does not exist.
    """
    return recompute_memory_atom(atom_id)


@mcp.tool()
def memory_get_signals(
    memory_atom_id: str,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Return the signals linked to a memory atom for provenance inspection.

    Each signal is an immutable evidence record that contributed to the atom's
    current aggregated confidence, support_weight, opposition_weight, and
    disagreement_score. Use this to understand *why* an atom has its current
    aggregate state — to see which sources agree or conflict, and how recent
    the evidence is.

    Particularly useful when disagreement_flag is true on an atom: inspect
    the signals to see which sources are in conflict and decide whether to
    update or keep the atom.

    Read-only. raw_input is excluded from output. No writes.

    Args:
        memory_atom_id: UUID of the memory atom to inspect.
        limit: Maximum signals to return. Clamped to 1–100. Default 20.

    Returns:
        List of signal dicts ordered by created_at DESC. Empty list if the
        atom has no linked signals or does not exist.
    """
    return get_atom_signals(memory_atom_id=memory_atom_id, limit=limit)


@mcp.tool()
def memory_project_kickoff(
    project_name: str,
    discussion: str,
) -> dict[str, Any]:
    """Extract architectural decisions from a design discussion and store them
    under a named project scope.

    Call this at the start of a new project (or after a major design discussion)
    to capture tech stack choices, constraints, naming conventions, and
    architectural decisions so they persist across sessions and agents.

    The stored atoms surface automatically in future sessions via
    memory_project_context(scope='project:<slug>') or memory_search with the
    same scope filter.  Scope is the hard isolation boundary between projects —
    two projects with different slugs never contaminate each other regardless
    of embedding similarity.

    Scope convention:
        scope = "project:<slug>"
        project_name is lowercased and non-alphanumeric characters become '-'.
        e.g. "My Rails App" → scope = "project:my-rails-app"

    After kickoff, retrieve the stored context with:
        memory_project_context(scope="project:<slug>")

    Args:
        project_name: Human-readable project name.  Normalised to a slug.
        discussion: The design discussion text to process — paste the relevant
            portion of the planning conversation or a written design brief.

    Returns:
        {
            "scope":              str,   # e.g. "project:my-rails-app"
            "stored":             list,  # atoms written
            "skipped_duplicates": int,
            "count":              int,
        }
    """
    return kickoff_project(project_name=project_name, discussion=discussion)


@mcp.tool()
def memory_assess_task_readiness(
    task_description: str,
    project_scope: str,
    model_scope: str | None = None,
    task_hint: str | None = None,
    user_requested_web: bool = False,
) -> dict[str, Any]:
    """Evaluate whether the agent has sufficient context to proceed with a task.

    Calls memory_task_context internally then applies deterministic readiness
    rules. Returns a structured verdict so the agent can decide to proceed,
    search the web, ask the user, inspect conflicts, or define tests first.

    Call this immediately after memory_task_context to gate the task.

    Recommended_action values and what to do:
    - "proceed"         — context is sufficient; begin the task
    - "project_kickoff" — call memory_project_kickoff first to bootstrap context
    - "inspect_conflict"— review contested atoms in risks[] before coding
    - "search_web"      — retrieve current information before proceeding
    - "define_tests"    — clarify test/verification requirements with user first
    - "retrieve_more"   — call memory_search with suggested_search_query
    - "ask_user"        — ask the user for the missing context listed in missing[]

    Do not proceed if ready=false.

    Args:
        task_description: One-sentence description of the task to be performed.
        project_scope: Project scope (e.g. 'project:memory-layer').
        model_scope: Optional model scope (e.g. 'model:qwen3-8b'). Fetches
            model lessons so known weaknesses affect the verdict.
        task_hint: Optional hint for semantic search over project atoms.
            Use the same hint as passed to memory_task_context.
        user_requested_web: True if the user explicitly asked for web/current
            information.
    """
    return assess_task_readiness_handler(
        task_description=task_description,
        project_scope=project_scope,
        model_scope=model_scope,
        task_hint=task_hint,
        user_requested_web=user_requested_web,
    )


@mcp.tool()
def memory_reflect_turn(
    user_msg: str,
    answer: str,
    thinking: str = "",
    scope: str | None = None,
) -> dict:
    """Reflect on a full Copilot turn and commit durable insights to memory.

    Pass the user message, assistant answer, and optionally the assistant's
    raw chain-of-thought (<think> block content).  The LLM judges what is
    worth storing across all three sources, then each candidate passes through
    the commit pipeline (reconcile → critic → risk gate) before any write.

    Call this at the end of a turn when the conversation produced new facts,
    decisions, preferences, or lessons that should persist across sessions.
    Do NOT call it for purely ephemeral exchanges (e.g. quick lookups).

    Args:
        user_msg: The user's message for this turn.
        answer: The assistant's final response (think-block content already
                stripped — pass clean answer text only).
        thinking: Chain-of-thought text from <think>...</think> blocks if the
                  model exposed them.  Empty string if not available.
        scope: Optional scope override for all atoms committed this turn
               (e.g. 'project:memory-layer').  When omitted the extractor
               assigns scope per candidate.

    Returns:
        {
            "committed": [{atom_id, final_memory_text, memory_type, scope, write_action}, ...],
            "proposed":  [{candidate_text, proposal_id, reason}, ...],
            "skipped":   int,
        }
        On extraction failure returns {"error": "<message>", "committed": [], ...}.
    """
    return _reflect_turn_impl(
        user_msg=user_msg,
        answer=answer,
        thinking=thinking,
        scope=scope,
    )


@mcp.tool()
def memory_ingest_transcript(
    turns: list[dict],
    source_label: str = "external",
    scope: str | None = None,
) -> dict:
    """Import a conversation transcript from any LLM and commit durable memories.

    Use this to share knowledge across LLMs — paste turns from a Claude, GPT-4,
    or Gemini conversation and the system will extract what is worth remembering,
    reconcile it against what it already knows, and store it in the shared brain.

    Each candidate passes through the full commit pipeline (reconcile → critic
    → risk gate) before any write. Nothing is stored silently.

    Args:
        turns: List of {"role": "user"|"assistant", "content": str} dicts in
               chronological order. Requires at least one user turn.
        source_label: Name of the originating LLM or tool. Stored in
                      memory_signals.source_key for provenance and future
                      trust weighting. Examples: \"claude-3-7-sonnet\",
                      \"gpt-4o\", \"gemini-pro\", \"local_user\".
        scope: Optional scope override for all extracted atoms. Omit to let
               the extractor assign scope per-candidate — cross-project user
               preferences will naturally land in the \"user\" scope.

    Returns:
        {
            \"committed\":       list of stored atoms,
            \"proposed\":        list of candidates queued for human review,
            \"skipped\":         int,
            \"turns_processed\": int,
            \"errors\":          list of non-fatal per-turn error messages
        }
    """
    return _ingest_impl(turns=turns, source_label=source_label, scope=scope)


@mcp.tool()
def memory_get_belief(
    atom_id: str | None = None,
    query: str | None = None,
    scope: str | None = None,
) -> dict:
    """Return the full belief picture for a memory atom.

    Combines the current atom state, its supporting and opposing evidence
    signals, and the complete revision history (birth → refine → supersede
    → reinforce events). Use this when you need to inspect *why* the system
    holds a belief, how confident it is, what challenges it, and how it
    evolved over time.

    Exactly one of atom_id or query must be provided.
    If query is given, a semantic search is run first and the top match is used.

    Args:
        atom_id: UUID of the memory atom to inspect directly.
        query:   Natural language question; top semantic match is used.
        scope:   Optional scope filter when using query
                 (e.g. 'user', 'project:myapp').

    Returns:
        {
            "atom":             {...},    # current belief state
            "signals": {
                "supporting": [...],     # evidence that reinforces the belief
                "opposing":   [...],     # evidence that challenges it
            },
            "revision_history": [...],   # oldest-first chain of belief changes
            "belief_summary":   str,     # plain-English one-liner for quick use
        }
        or {"error": "<message>"} if nothing is found.
    """
    return _get_belief_impl(atom_id=atom_id, query=query, scope=scope)


@mcp.tool()
def memory_log_turn(
    user_message: str,
    assistant_response: str,
    retrieved_atom_ids: list[str] | None = None,
    used_atom_ids: list[str] | None = None,
    context_status: str = "sufficient",
    verdict: str = "approved",
    confidence: float = 0.85,
    reasoning: str = "",
) -> dict:
    """Log one AI conversation turn to the prompt-history audit tables.

    Call this at the end of any turn where you retrieved or stored memories so
    the turn is visible in the dashboard under Prompt History.

    Args:
        user_message:       The user's raw prompt for this turn.
        assistant_response: The final answer you gave the user.
        retrieved_atom_ids: UUIDs of all atoms fetched this turn via
                            memory_search / memory_get / memory_recent / etc.
                            Pass [] if no atoms were retrieved.
        used_atom_ids:      Subset of retrieved_atom_ids that actually shaped
                            the response.  Pass [] if memory was not used, or
                            the same list as retrieved_atom_ids if all were used.
        context_status:     Quality verdict on the retrieved context.
                            'sufficient' | 'insufficient' | 'stale' |
                            'conflicting' | 'unsupported' | 'needs_verification' |
                            'needs_user_clarification' | 'unsafe_or_blocked'
        verdict:            Response quality verdict.
                            'approved' | 'needs_caveat' | 'needs_revision' |
                            'needs_verification' | 'blocked'
        confidence:         Confidence in the response, 0.0–1.0.
        reasoning:          Short note on how memory influenced this response.

    Returns:
        {"response_trace_id": str, "context_trace_id": str, "status": "logged"}
    """
    return _log_turn_impl(
        user_message=user_message,
        assistant_response=assistant_response,
        retrieved_atom_ids=retrieved_atom_ids,
        used_atom_ids=used_atom_ids,
        context_status=context_status,
        verdict=verdict,
        confidence=confidence,
        reasoning=reasoning,
    )


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Memory Layer MCP Server")
    parser.add_argument(
        "--transport",
        default="stdio",
        choices=["stdio", "streamable-http", "sse"],
        help="Transport to use (default: stdio)",
    )
    parser.add_argument("--port", type=int, default=8765,
                        help="Port for HTTP transports (default: 8765)")
    parser.add_argument("--host", default="0.0.0.0",
                        help="Bind host for HTTP transports (default: 0.0.0.0)")
    args, _ = parser.parse_known_args()

    if args.transport == "stdio":
        print("Starting memoryLayer MCP server (stdio)...", file=sys.stderr)
        mcp.run(transport="stdio")
    else:
        print(f"Starting memoryLayer MCP server ({args.transport} {args.host}:{args.port})...",
              file=sys.stderr)
        mcp.run(transport=args.transport, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
