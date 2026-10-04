"""Offline tests: private-by-default writes, required scope, per-identity
source decay, and two-sided conflict rendering in memory_task_context."""
from __future__ import annotations

from datetime import datetime, timezone

from app.commit_pipeline import _effective_visibility
from app.retrieval_policy import access_clause, is_conflicted
from app.signal_aggregator import compute_atom_weights


# ── visibility ────────────────────────────────────────────────────────────────

def test_visibility_defaults_to_private():
    assert _effective_visibility("", {}) == "private"
    assert _effective_visibility(None, {"suggested_visibility": "public"}) == "private"
    assert _effective_visibility("bogus", {}) == "private"


def test_critic_can_restrict_but_not_widen():
    assert _effective_visibility("public", {"suggested_visibility": "private"}) == "private"
    assert _effective_visibility("private", {"suggested_visibility": "public"}) == "private"
    assert _effective_visibility("team", {"suggested_visibility": "public"}) == "team"
    assert _effective_visibility("public", {"suggested_visibility": "public"}) == "public"


def test_critic_private_verdict_survives_parsing():
    """The parser used to drop suggested_visibility, so the critic's
    sensitive-content override never reached _effective_visibility."""
    from app.commit_pipeline import _parse_critic_response

    raw = '{"decision":"commit","final_memory_text":"x","suggested_visibility":"private"}'
    critic = _parse_critic_response(raw, "x", "fact", "user", 0.8)
    assert _effective_visibility("public", critic) == "private"

    raw = '{"decision":"commit","final_memory_text":"x","suggested_visibility":"public"}'
    critic = _parse_critic_response(raw, "x", "fact", "user", 0.8)
    assert _effective_visibility("team", critic) == "team"


def test_store_auto_requires_scope(monkeypatch):
    from mcp_server.tools import store_auto

    def _boom(*a, **k):
        raise AssertionError("pipeline must not run without a scope")

    monkeypatch.setattr(store_auto, "MemoryCommitPipeline", _boom)
    for scope in (None, "", "   "):
        out = store_auto.store_memory_auto(
            content="The billing service uses Postgres.",
            memory_type="decision",
            relationship="new",
            scope=scope,
        )
        assert out["stored"] is False
        assert "scope is required" in out["rejection_reason"]


def test_store_auto_passes_private_by_default(monkeypatch):
    from mcp_server.tools import store_auto

    seen = {}

    class _Decision:
        def to_dict(self):
            return {"committed_atom_id": "a", "committed_signal_id": "s"}

    class _Pipeline:
        def commit_candidate(self, candidate, source_user_id=None, visibility=None):
            seen["visibility"] = visibility
            return _Decision()

    monkeypatch.setattr(store_auto, "MemoryCommitPipeline", _Pipeline)
    store_auto.store_memory_auto(
        content="The billing service uses Postgres.",
        memory_type="decision",
        relationship="new",
        scope="project:billing",
    )
    assert seen["visibility"] == "private"


# ── access rule ───────────────────────────────────────────────────────────────

def test_access_clause_none_for_local_mode():
    assert access_clause(None) == ("", ())


def test_access_clause_checks_owner_public_and_team_membership():
    sql, params = access_clause("alice", alias="a")
    assert "a.visibility = 'public'" in sql
    assert "a.source_user_id = %s" in sql
    assert "scope_members" in sql
    assert params == ("alice", "alice")


# ── signal aggregation ───────────────────────────────────────────────────────

def test_two_users_through_one_client_are_two_sources():
    now = datetime.now(timezone.utc)
    sigs = [
        {"relationship": "new", "confidence": 0.8, "source_key": "local_user",
         "source_user_id": "alex", "created_at": now},
        {"relationship": "conflict", "confidence": 0.8, "source_key": "local_user",
         "source_user_id": "jordan", "created_at": now},
    ]
    w = compute_atom_weights(sigs, memory_type="decision")
    # Equal weight on each side — jordan's claim is not halved as a repeat.
    assert abs(w["support_weight"] - w["opposition_weight"]) < 1e-6
    assert w["disagreement_score"] > 0.4
    assert w["unique_source_count"] == 2


def test_public_pool_counts_one_tool_as_one_source():
    now = datetime.now(timezone.utc)
    burst = [
        {"relationship": "new", "confidence": 0.8, "source_key": "local_user",
         "source_user_id": f"new-account-{i}", "created_at": now}
        for i in range(5)
    ]
    team = compute_atom_weights(burst, memory_type="fact", visibility="team")
    public = compute_atom_weights(burst, memory_type="fact", visibility="public")
    # Team: five people, full weight each. Public: five accounts through one
    # tool decay geometrically, so the burst cannot buy corroboration.
    assert abs(team["support_weight"] - 4.0) < 1e-6
    assert public["support_weight"] < 1.6
    assert public["unique_source_count"] == 5  # still recorded for forensics


def test_is_conflicted_threshold():
    assert is_conflicted({"lifecycle_status": "contested"})
    assert is_conflicted({"disagreement_score": 0.41})
    assert not is_conflicted({"disagreement_score": 0.4})


# ── memory_task_context rendering ─────────────────────────────────────────────

class _FakeStore:
    def __init__(self):
        self.calls = []

    def project_context_atoms(self, scope, limit, min_importance, min_confidence, requesting_user=None):
        self.calls.append(("project_context_atoms", scope, requesting_user))
        if scope == "user":
            return [{"id": "u1", "content": "User prefers short answers.", "memory_type": "preference",
                     "confidence": 0.9, "disagreement_score": 0.0}]
        return [{"id": "p1", "content": "The team picked Postgres.", "memory_type": "decision",
                 "confidence": 0.6, "disagreement_score": 0.5, "lifecycle_status": "contested"}]

    def get_active_atoms_by_scope(self, scope, limit, requesting_user=None):
        return []

    def list_task_runs_db(self, scope, limit):
        return []

    def retrieve_memories(self, **kwargs):
        self.calls.append(("retrieve_memories", kwargs))
        return []

    def attach_conflicts(self, atoms):
        for a in atoms:
            if is_conflicted(a):
                a["conflict"] = {
                    "supporting": [{"source": "alex", "created_at": "2026-09-01T00:00:00",
                                    "content": "The team picked Postgres."}],
                    "opposing": [{"source": "jordan", "created_at": "2026-09-03T00:00:00",
                                  "content": "The team picked SQLite."}],
                }
        return atoms


def test_task_context_reports_both_claims_and_user_scope(monkeypatch):
    from mcp_server.tools import task_context
    from mcp_server.auth_context import current_user_id

    fake = _FakeStore()
    monkeypatch.setattr(task_context, "get_store", lambda: fake)
    token = current_user_id.set("alex")
    try:
        out = task_context.get_task_context(
            project_scope="project:billing", task_hint="which database did we pick?"
        )
    finally:
        current_user_id.reset(token)

    contested = out["project_context"][0]
    assert "CONTESTED" in contested
    assert "alex (for, 2026-09-01): The team picked Postgres." in contested
    assert "jordan (against, 2026-09-03): The team picked SQLite." in contested
    assert out["user_context"] == ["[preference] (0.90) User prefers short answers."]

    # Every read runs as the caller, and semantic search covers project + user.
    assert all(c[2] == "alex" for c in fake.calls if c[0] == "project_context_atoms")
    search = next(c[1] for c in fake.calls if c[0] == "retrieve_memories")
    assert search["scope_filters"] == ["project:billing", "user"]
    assert search["requesting_user"] == "alex"
    assert "project:billing" in out["write_protocol"]
