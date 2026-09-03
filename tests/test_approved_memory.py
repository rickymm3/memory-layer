"""Authority-boundary tests for public knowledge consumers."""
from __future__ import annotations

from app.sqlite_store import SQLiteStore
from mcp_server.tools import search as search_tools
from mcp_server.tools import store_approved as approved_tools


def test_approved_search_excludes_unreviewed_and_private_atoms(store: SQLiteStore, monkeypatch):
    store.store_memory_with_signal(
        "Whiplash supports four players.",
        scope="project:whiplash",
        visibility="public",
    )
    approved_id, _ = store.store_memory_with_signal(
        "Whiplash supports local cooperative play.",
        scope="project:whiplash",
        confidence=0.95,
        visibility="public",
        authority_status="approved",
        authority_reviewer="editor@example.com",
    )
    store.store_memory_with_signal(
        "A private approved note must not reach the public consumer.",
        scope="project:whiplash",
        confidence=0.95,
        visibility="private",
        authority_status="approved",
        authority_reviewer="editor@example.com",
    )
    monkeypatch.setattr(search_tools, "get_store", lambda: store)

    response = search_tools.search_approved_memories(
        query="Whiplash supports local cooperative play.",
        limit=20,
        scope="project:whiplash",
        min_similarity=0.0,
        min_confidence=0.0,
        max_disagreement=1.0,
    )
    results = response["results"]

    assert [item["id"] for item in results] == [approved_id]
    assert results[0]["authority_status"] == "approved"
    assert results[0]["authority_reviewer"] == "editor@example.com"
    assert store.get_approved_memory_revision("project:whiplash")


def test_approved_search_excludes_nonactive_atoms(store: SQLiteStore):
    atom_id, _ = store.store_memory_with_signal(
        "A previously approved platform claim.",
        scope="project:whiplash",
        visibility="public",
        authority_status="approved",
    )
    with store._connect() as conn:
        conn.execute(
            "UPDATE memory_atoms SET lifecycle_status='contested' WHERE id=?;",
            (atom_id,),
        )

    results = store.search_memories_full(
        query="A previously approved platform claim.",
        scope="project:whiplash",
        min_similarity=0.0,
        lifecycle_status="active",
        authority_status="approved",
        min_confidence=0.0,
        max_disagreement=1.0,
        visibility="public",
    )
    assert results == []


def test_approved_tool_requires_explicit_project_scope(store: SQLiteStore, monkeypatch):
    monkeypatch.setattr(search_tools, "get_store", lambda: store)
    result = search_tools.search_approved_memories("game modes", scope="user")
    assert result["results"] == []
    assert "project:<name>" in result["error"]


def test_human_approval_path_marks_atom_authoritative(store: SQLiteStore, monkeypatch):
    proposal_id = store.store_proposal(
        content="Whiplash has an approved game overview.",
        memory_type="fact",
        relationship="new",
        scope="project:whiplash",
        confidence=0.95,
        visibility="public",
        source_user_id="submitter-1",
    )
    with store._connect() as conn:
        conn.execute(
            "UPDATE memory_proposals SET status='approved', approval_token='test-token', "
            "token_expires_at='2099-01-01T00:00:00+00:00' WHERE id=?;",
            (proposal_id,),
        )
    monkeypatch.setattr(approved_tools, "get_store", lambda: store)

    result = approved_tools.store_memory_approved(
        proposal_id,
        "test-token",
        authority_reviewer="editor-1",
    )

    assert result["stored"] is True
    assert result["authority_status"] == "approved"
    atom = store.get_atom_with_signals(result["memory_atom_id"])
    assert atom["authority_status"] == "approved"
    assert atom["source_type"] == "reviewed_proposal"
    assert atom["visibility"] == "public"
    assert atom["authority_reviewer"] == "editor-1"


def test_mcp_auto_store_wrapper_calls_handler_without_recursion(monkeypatch):
    from mcp_server import server

    monkeypatch.setattr(server, "_store_memory_auto", lambda **kwargs: kwargs)
    result = server.memory_store_auto(
        content="A fact",
        memory_type="fact",
        relationship="new",
    )
    assert result["content"] == "A fact"
    assert result["memory_type"] == "fact"
