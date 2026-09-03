"""MCP server for memory-layer — stdio and SSE/HTTP modes.

Twelve tools — the full working surface:
  memory_health       : check DB + Ollama reachability
  memory_search       : semantic similarity search
  memory_search_approved: approved-only project search for public consumers
  memory_store_auto   : write a memory atom through the full commit pipeline
  memory_propose_signal: queue a candidate without writing authoritative memory
  memory_store_approved: commit a human-approved proposal with an approval token
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
from mcp.types import ToolAnnotations, PromptMessage, TextContent

from mcp_server.tools.get import get_memory_by_id
from mcp_server.tools.health import get_memory_health
from mcp_server.tools.task_context import get_task_context
from mcp_server.tools.search import search_memories, search_approved_memories
from mcp_server.tools.store_auto import store_memory_auto as _store_memory_auto
from mcp_server.tools.propose_signal import propose_memory_signal
from mcp_server.tools.store_approved import store_memory_approved as _store_memory_approved
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


@mcp.tool(annotations=ToolAnnotations(title="Memory Health Check", readOnlyHint=True))
def memory_health() -> dict[str, Any]:
    """Check memory-layer health: DB reachability, Ollama reachability, atom count.

    Never exposes secrets, connection strings, file paths, or stack traces.
    For a full corpus health report use memory_audit.
    """
    return get_memory_health()


@mcp.tool(annotations=ToolAnnotations(title="Search Memories", readOnlyHint=True))
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


@mcp.tool(annotations=ToolAnnotations(title="Search Approved Memories", readOnlyHint=True))
def memory_search_approved(
    query: str,
    scope: str,
    limit: int = 8,
    memory_type: str | None = None,
    min_similarity: float = 0.45,
    min_confidence: float = 0.70,
    max_disagreement: float = 0.35,
) -> dict[str, Any]:
    """Search only human-approved, active, public memory in one project scope.

    This is the safe read contract for public websites and other constrained
    consumers. Results exclude unreviewed, rejected, contested, superseded,
    deprecated, archived, low-confidence, and highly disputed atoms.
    """
    return search_approved_memories(
        query=query,
        scope=scope,
        limit=limit,
        memory_type=memory_type,
        min_similarity=min_similarity,
        min_confidence=min_confidence,
        max_disagreement=max_disagreement,
    )


@mcp.tool(annotations=ToolAnnotations(title="Store Memory", readOnlyHint=False, destructiveHint=True))
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
    visibility: str = "public",
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
        visibility: Access boundary: private | team | public. Default public. Override to private only for passwords, PII, or sensitive personal details.
    """
    # Priority: explicit arg → SSE auth context → MEMORY_USER_ID env var (stdio mode)
    from mcp_server.auth_context import current_user_id as _uid_ctx  # noqa: PLC0415
    effective_user = source_user_id or _uid_ctx.get() or os.environ.get("MEMORY_USER_ID")

    return _store_memory_auto(
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


@mcp.tool(annotations=ToolAnnotations(title="Propose Memory Evidence", readOnlyHint=False))
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
    visibility: str = "private",
) -> dict[str, Any]:
    """Queue untrusted evidence for review without creating a memory atom."""
    from mcp_server.auth_context import current_user_id as _uid_ctx  # noqa: PLC0415
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
        visibility=visibility,
        source_user_id=_uid_ctx.get() or os.environ.get("MEMORY_USER_ID"),
    )


@mcp.tool(annotations=ToolAnnotations(
    title="Store Approved Memory",
    readOnlyHint=False,
    destructiveHint=True,
))
def memory_store_approved(
    proposal_id: str,
    approval_token: str,
) -> dict[str, Any]:
    """Commit a proposal after the human review CLI issues a short-lived token."""
    from mcp_server.auth_context import current_user_id as _uid_ctx  # noqa: PLC0415
    return _store_memory_approved(
        proposal_id=proposal_id,
        approval_token=approval_token,
        authority_reviewer=_uid_ctx.get() or os.environ.get("MEMORY_USER_ID") or "human_review",
    )


@mcp.tool(annotations=ToolAnnotations(title="Get Memory by ID", readOnlyHint=True))
def memory_get(memory_id: str) -> dict[str, Any] | None:
    """Fetch a single memory atom by UUID, including its signals summary.

    Returns the full atom record with content, confidence, scope, lifecycle
    status, signal count, and support/opposition weights. Returns null if the
    UUID does not exist.

    Args:
        memory_id: UUID string of the memory atom to fetch.
    """
    return get_memory_by_id(memory_id)


@mcp.tool(annotations=ToolAnnotations(title="Load Session Context", readOnlyHint=True))
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


@mcp.tool(annotations=ToolAnnotations(title="Audit Memory Corpus", readOnlyHint=True))
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


@mcp.tool(annotations=ToolAnnotations(title="Link Two Memories", readOnlyHint=False, destructiveHint=True))
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


@mcp.tool(annotations=ToolAnnotations(title="Find Related Memories", readOnlyHint=True))
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


@mcp.tool(annotations=ToolAnnotations(title="Ingest Conversation", readOnlyHint=False, destructiveHint=True))
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


_START_SESSION_TEXT = """\
🧠 **Starting a Synapse memory session**

Before we begin — a quick heads-up on what this connection does:

• What you share may be stored as memory atoms (your beliefs, preferences, decisions, experiences, facts)
• Atoms can contribute to public posts on Synapse, visible to other users
• Passwords, API keys, and private personal details are kept private automatically
• You can say "keep this private" at any point to exclude something from storage

Selecting this prompt means you're good with this for our conversation.
If you want to push a conversation retroactively instead, use /push_to_synapse.

---

Please call memory_task_context now with:
  project_scope = "project:memory-layer"
  model_scope   = "model:claude-sonnet-4-6"
  task_hint     = "general session — user will direct"

Then confirm you're loaded and ready.

---

**Your two roles in this session:**

**1. Memory writer — mandatory after every turn.**
Call memory_store_auto after ANY turn where the user shared something worth remembering:
facts, opinions, experiences, project details, plans, corrections, preferences — all of it.
Do NOT skip a turn because it felt "informational." Information IS what gets stored.
The only turns that don't need a write are pure pleasantries ("ok", "thanks", "got it").
Every write must include WHAT + WHY + CONTEXT. Entity-level facts (names, numbers,
mechanics, rules about things the user created) must be preserved verbatim — do not
compress them into abstract summaries.

**2. Conversation coach — guide toward post-worthy depth.**
After each response, briefly assess whether this conversation has enough to generate a
good post: at least 3 distinct opinions, experiences, or facts on the same topic.
If it's thin, naturally invite more — one follow-up question that would help:
  • Ask for their opinion on a specific angle: "What's your take on X?"
  • Ask for a concrete example: "Has that come up in your own work?"
  • Ask what they actually believe: "Do you think that's the right call?"
Keep it conversational — one question woven into your response, not a checklist.
If the conversation already has depth, don't force it — let it flow.\
"""

_PUSH_TEXT = """\
📤 **Push this conversation to Synapse**

You're about to send this conversation retroactively to the Synapse memory layer.

What will happen:
• The conversation is analyzed for durable memories — beliefs, decisions, preferences
• Extracted atoms are stored as public by default
• Passwords and private details are automatically kept private
• Atoms may contribute to or update public posts on Synapse

By proceeding you're consenting to this for the current conversation.

---

Please call memory_push_conversation now.
Pass the conversation transcript as the "transcript" argument.
Set is_jsonl_path = false.\
"""


@mcp.prompt()
def start_session() -> list[PromptMessage]:
    """Start a memory-enabled session: consent disclosure + load your Synapse context."""
    return [PromptMessage(role="user", content=TextContent(type="text", text=_START_SESSION_TEXT))]


@mcp.prompt()
def push_to_synapse() -> list[PromptMessage]:
    """Retroactively push this conversation to Synapse. Use when you forgot to start a session."""
    return [PromptMessage(role="user", content=TextContent(type="text", text=_PUSH_TEXT))]


def main() -> None:
    """Run the MCP server using the configured transport."""
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


if __name__ == "__main__":
    main()
