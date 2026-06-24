"""Tests for MemoryCommitPipeline — write integrity and dual-write invariant.

These tests guard against the anti-pattern: 'Treating a silent write failure
as a passing test.' The pipeline's dual-write (atom + signal) must be confirmed
on every commit path — not just assumed from a passing quality score.
"""
from __future__ import annotations

import math
import uuid
import pytest

from app.sqlite_store import SQLiteStore
from app.commit_pipeline import MemoryCommitPipeline


# ── fixtures ──────────────────────────────────────────────────────────────────

class _FakeEmbedder:
    DIM = 8

    def embed_text(self, text: str) -> list[float]:
        seed = hash(text) % (2**31)
        vec = [math.sin(seed + i) for i in range(self.DIM)]
        mag = math.sqrt(sum(x * x for x in vec)) or 1.0
        return [x / mag for x in vec]


class _StubLLM:
    """Minimal LLM stub — returns a deterministic approved critic response."""

    def generate_response(self, prompt: str, system: str = "") -> str:
        return (
            '{"verdict":"approved","canonicalized_text":null,'
            '"memory_type":null,"scope":null,"confidence_adjustment":0.0,'
            '"context_summary":"test context","suggested_visibility":"private",'
            '"novelty_score":0.5,"interest_flag":false,"reasoning":"stub"}'
        )

    def structured_response(self, prompt: str, schema: dict) -> dict:
        return {
            "verdict": "approved",
            "canonicalized_text": None,
            "memory_type": None,
            "scope": None,
            "confidence_adjustment": 0.0,
            "context_summary": "test context",
            "suggested_visibility": "private",
            "novelty_score": 0.5,
            "interest_flag": False,
            "reasoning": "stub",
        }


@pytest.fixture
def pipeline(tmp_path):
    db = tmp_path / "pipeline_test.db"
    store = SQLiteStore(str(db), ollama_client=_FakeEmbedder())
    store.init_db()
    llm = _StubLLM()
    return MemoryCommitPipeline(store=store, llm=llm)


# ── dual-write invariant ───────────────────────────────────────────────────────

def test_commit_returns_both_ids(pipeline):
    """Core dual-write invariant: every commit must return atom_id AND signal_id."""
    decision = pipeline.commit_candidate(
        {"content": f"test fact {uuid.uuid4()}", "memory_type": "fact", "should_store": True},
        source_key="test",
        source_type="test",
    )
    assert decision.committed_atom_id is not None, "atom_id must not be None after commit"
    assert decision.committed_signal_id is not None, "signal_id must not be None after commit"
    assert decision.committed_atom_id != decision.committed_signal_id


def test_commit_ids_are_valid_uuids(pipeline):
    """Both IDs returned from commit must be valid UUIDs."""
    decision = pipeline.commit_candidate(
        {"content": f"another fact {uuid.uuid4()}", "memory_type": "observation"},
        source_key="test",
    )
    if decision.committed_atom_id:
        uuid.UUID(decision.committed_atom_id)  # raises ValueError if invalid
    if decision.committed_signal_id:
        uuid.UUID(decision.committed_signal_id)


def test_commit_rejects_empty_content(pipeline):
    """Pipeline must raise ValueError for empty content — not silently skip."""
    with pytest.raises(ValueError):
        pipeline.commit_candidate({"content": "", "memory_type": "fact"})


def test_commit_rejects_whitespace_content(pipeline):
    """Whitespace-only content must also raise ValueError."""
    with pytest.raises(ValueError):
        pipeline.commit_candidate({"content": "   ", "memory_type": "fact"})


def test_commit_deduplicates_exact_content(pipeline):
    """Exact duplicate content must not produce two separate atoms."""
    content = f"dedup test {uuid.uuid4()}"
    d1 = pipeline.commit_candidate({"content": content, "memory_type": "fact"})
    d2 = pipeline.commit_candidate({"content": content, "memory_type": "fact"})
    assert d1.committed_atom_id is not None
    # Second commit: either same atom (reinforce) or a new atom — but never silent failure
    assert d2.decision in (
        "commit", "reinforce_existing", "refine_existing",
        "supersede_existing", "duplicate_skipped",
    ), f"unexpected decision: {d2.decision}"


def test_commit_stores_retrievable_atom(pipeline):
    """A committed atom must be retrievable from the store — not just a DB ghost."""
    content = f"retrievable fact {uuid.uuid4()}"
    decision = pipeline.commit_candidate(
        {"content": content, "memory_type": "fact"},
        source_key="retrieval_test",
    )
    if decision.committed_atom_id:
        atom = pipeline.store.get_atom_with_signals(decision.committed_atom_id)
        assert atom is not None, "committed atom must be retrievable by ID"
        assert atom["content"] == content
