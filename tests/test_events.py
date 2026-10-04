"""Commit events: the core emits, layers subscribe, handler failures never
break a write."""
from __future__ import annotations

import math

import pytest

from app import events
from app.commit_pipeline import MemoryCommitPipeline
from app.sqlite_store import SQLiteStore


class _Emb:
    def embed_text(self, text):
        seed = hash(text) % (2**31)
        v = [math.sin(seed + i) for i in range(8)]
        n = math.sqrt(sum(x * x for x in v))
        return [x / n for x in v]


class _LLM:
    def generate_response(self, prompt, system="", **kw):
        return '{"decision":"commit","final_memory_text":null,"novelty_score":0.9}'


@pytest.fixture
def pipeline(tmp_path):
    store = SQLiteStore(str(tmp_path / "ev.db"), ollama_client=_Emb())
    store.init_db()
    return MemoryCommitPipeline(store=store, llm=_LLM())


@pytest.fixture
def captured():
    seen = []

    def handler(**kw):
        seen.append(kw)

    events.on("atom_committed", handler)
    yield seen
    events.off("atom_committed", handler)


def test_commit_emits_atom_committed(pipeline, captured):
    d = pipeline.commit_candidate(
        {"content": "Invoices are stored in Postgres.", "scope": "project:billing"},
        source_user_id="alex",
        visibility="team",
    )
    assert len(captured) == 1
    ev = captured[0]
    assert ev["atom_id"] == d.committed_atom_id
    assert ev["source_user_id"] == "alex"
    assert ev["scope"] == "project:billing"
    assert ev["visibility"] == "team"
    assert ev["decision"] == "commit"
    assert ev["novelty_score"] == 0.9 and ev["interest_flag"] is True


def test_failing_handler_does_not_break_the_write(pipeline, captured):
    def boom(**kw):
        raise RuntimeError("layer bug")

    events.on("atom_committed", boom)
    try:
        d = pipeline.commit_candidate(
            {"content": "Refunds go through Stripe.", "scope": "project:billing"}
        )
    finally:
        events.off("atom_committed", boom)
    assert d.committed_atom_id
    assert len(captured) == 1  # later handlers still ran


def test_plugins_env_loads_layer(monkeypatch, tmp_path):
    (tmp_path / "fake_layer.py").write_text(
        "from app.events import on\n"
        "CALLS = []\n"
        "on('atom_superseded', lambda **kw: CALLS.append(kw))\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("MEMORY_LAYER_PLUGINS", "fake_layer")
    monkeypatch.setattr(events, "_plugins_loaded", False)
    events.emit("atom_superseded", atom_id="a1", superseded_by="a2")
    import fake_layer
    assert fake_layer.CALLS == [{"atom_id": "a1", "superseded_by": "a2"}]


def test_synapse_registers_its_handlers():
    import synapse  # noqa: F401
    from synapse import hooks

    assert hooks.enqueue_post in events._handlers["atom_committed"]
    assert hooks.requeue_posts in events._handlers["atom_superseded"]
    assert hooks.publish_direct_answers in events._handlers["turn_reflected"]


def test_synapse_only_enqueues_public_atoms(monkeypatch):
    from synapse import hooks
    import synapse.post_worker as pw

    calls = []
    monkeypatch.setattr(pw, "enqueue", lambda *a: calls.append(a))
    hooks.enqueue_post(atom_id="a", source_user_id="u", scope="project:x", visibility="private")
    hooks.enqueue_post(atom_id="b", source_user_id="u", scope="project:x", visibility="public")
    assert calls == [("b", "u", "project:x")]
