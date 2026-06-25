"""MCP server for memory-layer — stdio and SSE/HTTP modes.

Eight tools — the full working surface:
  memory_health       : check DB + Ollama reachability
  memory_search       : semantic similarity search
  memory_store_auto   : write a memory atom through the full commit pipeline
  memory_get          : fetch a single atom by UUID (includes signals summary)
  memory_task_context : session-start compound snapshot
  memory_audit        : compound health + stale + duplicate report
  memory_link_atoms   : create an explicit relation between two atoms
  memory_related      : traverse the atom relations graph (1-3 hops)

Transport modes:
  stdio (default)     python -m mcp_server.server   / make mcp
  SSE / HTTP          MCP_TRANSPORT=sse make mcp-sse
                      MCP_PORT=8765 (default)

SSE auth: every request must include Authorization: Bearer <api_token>
          where api_token is the user's personal key from /settings.

All logs go to stderr. stdout is reserved for the MCP stdio protocol.
"""
from __future__ import annotations

import os
from typing import Any

from mcp.server.fastmcp import FastMCP

from mcp_server.tools.get import get_memory_by_id
from mcp_server.tools.health import get_memory_health
from mcp_server.tools.task_context import get_task_context
from mcp_server.tools.search import search_memories
from mcp_server.tools.store_auto import store_memory_auto
from mcp_server.tools.stale_atoms import get_stale_atoms
from mcp_server.tools.find_duplicates import find_duplicate_atoms
from mcp_server.tools.link_atoms import link_atoms
from mcp_server.tools.related_atoms import get_related_atoms
from mcp_server.tools.push_conversation import push_conversation_tool

_WRITE_PROTOCOL = """After any turn where the user expressed a preference, correction, decision, or instruction:
1. Call memory_store_auto BEFORE finishing your response — not at end-of-session.
2. Report both memory_atom_id and memory_signal_id.
3. Scope: project facts → 'project:<name>', model observations → 'model:claude-sonnet-4-6', user preferences → 'user'.
4. Content must be self-contained — no session-internal names like "Phase N" or "as discussed".
Triggers: preferences with reasons, architecture decisions, corrections, frustration/satisfaction signals."""

mcp = FastMCP("memoryLayer", instructions=_WRITE_PROTOCOL)


@mcp.tool()
def memory_health() -> dict[str, Any]:
    """Check memory-layer health: DB reachability, Ollama reachability, atom count.

    Never exposes secrets, connection strings, file paths, or stack traces.
    For a full corpus health report use memory_audit.
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
    similarity over pgvector. Returns content, confidence, scope, and composite
    score for each result.

    Args:
        query: Natural language question or topic to search for.
        limit: Maximum results to return. Clamped to 1–20. Default 5.
        scope: Optional scope filter (e.g. 'project:memory-layer', 'model:claude-sonnet-4-6').
        memory_type: Optional type filter (e.g. 'fact', 'decision', 'observation').
        min_similarity: Minimum cosine similarity (0.0–1.0). Default 0.0.
    """
    return search_memories(
        query=query,
        limit=limit,
        scope=scope,
        memory_type=memory_type,
        min_similarity=min_similarity,
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
    source_user_id: str | None = None,
    visibility: str = "private",
) -> dict[str, Any]:
    """Store a memory atom through the full commit pipeline.

    Runs write-quality scoring, reconciliation against existing atoms,
    critic review, and the risk gate before any write. Returns a canonical
    write report with atom_id, signal_id, decision, and quality metadata.
    Never stores silently.

    Scope discipline:
      project facts        → scope='project:<name>'
      model observations   → scope='model:<model-id>'
      user preferences     → scope='user'

    Args:
        content: Full canonical sentence of the memory to store.
        memory_type: Type: fact | decision | instruction | observation | preference | correction.
        relationship: Reconciler hint: new | refinement | reinforcement | conflict | opinion_change.
        context_summary: Compact prompt-friendly summary. Defaults to content.
        scope: Scope string (e.g. 'project:memory-layer', 'model:claude-sonnet-4-6', 'user').
        confidence: Float 0.0–1.0. Default 0.8.
        importance: Float 0.0–1.0. Default 0.5.
        reconciliation_reason: Reason string from reconciler output.
        matched_memory_ids: Related existing atom UUIDs.
        source_user_id: User identity for multi-user provenance tracking.
        visibility: Access boundary: private | team | public. Default private.
    """
    # Priority: explicit arg → SSE auth context → MEMORY_USER_ID env var (stdio mode)
    from mcp_server.auth_context import current_user_id as _uid_ctx  # noqa: PLC0415
    effective_user = source_user_id or _uid_ctx.get() or os.environ.get("MEMORY_USER_ID")

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
        source_user_id=effective_user,
        visibility=visibility,
    )


@mcp.tool()
def memory_get(memory_id: str) -> dict[str, Any] | None:
    """Fetch a single memory atom by UUID, including its signals summary.

    Returns the full atom record with content, confidence, scope, lifecycle
    status, signal count, and support/opposition weights. Returns null if the
    UUID does not exist.

    Args:
        memory_id: UUID string of the memory atom to fetch.
    """
    return get_memory_by_id(memory_id)


@mcp.tool()
def memory_task_context(
    project_scope: str,
    model_scope: str | None = None,
    task_hint: str | None = None,
    recent_tasks: int = 5,
    compact: bool = True,
) -> dict[str, Any]:
    """Session-start compound snapshot: project context + model lessons + task history.

    Call this once at the start of every session instead of calling
    memory_search + memory_get + memory_list_task_runs separately.

    Returns four sections:
    - project_context: high-importance, high-confidence project atoms
    - model_lessons: active atoms for the model (weaknesses, adaptations)
    - recent_task_runs: last N task outcomes for this project
    - task_relevant_atoms: semantic matches for task_hint (if provided)

    Args:
        project_scope: Required. E.g. 'project:memory-layer'.
        model_scope: Optional. E.g. 'model:claude-sonnet-4-6'. Returns
            known model behaviors and prompt adaptations.
        task_hint: Optional. Short task description — triggers semantic search
            and populates task_relevant_atoms (~500ms).
        recent_tasks: Recent task_run records to return. Clamped 1–20. Default 5.
        compact: True (default) returns '[type] (conf) content' strings (~80%
            fewer tokens). False returns full JSON dicts with metadata.
    """
    return get_task_context(
        project_scope=project_scope,
        model_scope=model_scope,
        task_hint=task_hint,
        recent_tasks=recent_tasks,
        compact=compact,
    )


@mcp.tool()
def memory_audit(
    scope: str | None = None,
    stale_days: int = 90,
    duplicate_threshold: float = 0.90,
) -> dict[str, Any]:
    """Compound corpus health report: stale atoms + near-duplicate pairs + basic stats.

    Replaces the three separate calls (memory_health, memory_stale_atoms,
    memory_find_duplicates) with one compound result. Use this to assess
    corpus quality before a cleanup pass or after a large write session.

    Returns:
    - health: DB + Ollama reachability, total atom count
    - stale_atoms: contested, unreinforced, or aged atoms needing review
    - duplicate_pairs: near-identical atom pairs (candidates for consolidation)
    - summary: counts of each category

    Args:
        scope: Restrict to a specific scope. Omit to audit all scopes.
        stale_days: Age threshold in days for low-support atoms. Default 90.
        duplicate_threshold: Cosine similarity cutoff for duplicates. Default 0.90.
    """
    health = get_memory_health()
    stale = get_stale_atoms(
        days_threshold=stale_days,
        scope=scope,
        limit=20,
    )
    dupes = find_duplicate_atoms(
        similarity_threshold=duplicate_threshold,
        scope=scope,
        limit=20,
    )

    return {
        "health": health,
        "stale_atoms": stale,
        "duplicate_pairs": dupes.get("pairs", []) if isinstance(dupes, dict) else dupes,
        "summary": {
            "total_atoms": health.get("atom_count", 0),
            "stale_count": len(stale),
            "duplicate_pair_count": len(dupes.get("pairs", [])) if isinstance(dupes, dict) else 0,
            "scope_filter": scope,
        },
    }


@mcp.tool()
def memory_link_atoms(
    atom_a_id: str,
    atom_b_id: str,
    relation_type: str = "related",
    confidence: float = 0.8,
) -> dict[str, Any]:
    """Create an explicit directed relation from atom_a to atom_b.

    Use this to wire together knowledge that is semantically related but
    not close enough in embedding space to be retrieved together automatically.
    Linked atoms are returned as 1-hop neighbors by memory_related.

    Valid relation_types: supports, contradicts, specializes, generalizes, related.

    Args:
        atom_a_id: UUID of the source atom.
        atom_b_id: UUID of the target atom.
        relation_type: Semantic relationship type. Default 'related'.
        confidence: Confidence in this relation 0.0–1.0. Default 0.8.
    """
    return link_atoms(
        atom_a_id=atom_a_id,
        atom_b_id=atom_b_id,
        relation_type=relation_type,
        confidence=float(confidence),
    )


@mcp.tool()
def memory_related(
    atom_id: str,
    depth: int = 1,
    relation_types: list[str] | None = None,
) -> dict[str, Any]:
    """Return atoms related to atom_id by traversing the explicit relations graph.

    Traverses memory_atom_relations bidirectionally up to `depth` hops.
    Use after memory_get or memory_search to pull in connected context
    that might be below similarity threshold in embedding space.

    Args:
        atom_id: UUID of the starting atom.
        depth: Number of hops to traverse (1–3). Default 1.
        relation_types: Optional filter list, e.g. ['supports', 'specializes'].
            Omit to follow all relation types.
    """
    return get_related_atoms(
        atom_id=atom_id,
        depth=depth,
        relation_types=relation_types,
    )


@mcp.tool()
def memory_push_conversation(
    transcript: str,
    is_jsonl_path: bool = False,
) -> dict[str, Any]:
    """Push an entire conversation into memory atoms.

    Processes all user→assistant turns in the transcript, extracts durable
    memory atoms from each substantive exchange, and commits them through the
    full write pipeline (reconciler + critic + risk gate).

    Post drafts are generated automatically in the background — check /drafts
    on the Synapse site for "Based on your conversation, here's a suggested post."

    Args:
        transcript: The conversation text. Either plain text with 'User:' /
            'Assistant:' role markers, or a path to a Claude Code .jsonl file
            when is_jsonl_path=True.
        is_jsonl_path: Set True when transcript is a filesystem path to a
            Claude Code session .jsonl file (e.g. ~/.claude/projects/.../*.jsonl).
    """
    from mcp_server.auth_context import current_user_id
    return push_conversation_tool(
        transcript=transcript,
        source_user_id=current_user_id(),
        is_jsonl_path=is_jsonl_path,
    )


if __name__ == "__main__":
    transport = os.environ.get("MCP_TRANSPORT", "stdio").lower()

    if transport == "sse":
        import uvicorn
        from mcp_server.token_middleware import TokenAuthMiddleware

        port = int(os.environ.get("MCP_PORT", "8765"))
        asgi_app = TokenAuthMiddleware(mcp.streamable_http_app())
        import sys
        print(f"memoryLayer MCP server — HTTP/SSE on port {port}", file=sys.stderr)
        print(f"Claude desktop URL: http://localhost:{port}/mcp", file=sys.stderr)
        uvicorn.run(asgi_app, host="0.0.0.0", port=port, log_level="warning")
    else:
        mcp.run(transport="stdio")
