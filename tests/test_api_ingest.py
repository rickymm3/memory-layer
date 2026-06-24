"""Smoke tests for the Flask write surface (/api/ingest).

These guard against the specific failure class found in the field: the
endpoint passing wrong argument types to commit_candidate, or reading
CommitDecision attributes incorrectly. A broken /api/ingest silently
drops all external LLM writes — the worst failure mode.
"""
from __future__ import annotations

import json
import uuid

import pytest

# ── helpers ────────────────────────────────────────────────────────────────────

class _FakeDecision:
    decision = "commit"
    committed_atom_id = str(uuid.uuid4())
    committed_signal_id = str(uuid.uuid4())
    confidence = 0.82


class _FakePipeline:
    def commit_candidate(self, candidate, **kwargs):
        assert isinstance(candidate, dict), "candidate must be a dict"
        assert "content" in candidate
        return _FakeDecision()


def _make_app(monkeypatch):
    """Build a minimal Flask test client with mocked pipeline and auth."""
    from app.config import get_config
    import os
    os.environ.setdefault("FLASK_SECRET_KEY", "test-secret-for-ci")
    os.environ.setdefault("DATABASE_URL", "")

    from app_main import create_app
    app = create_app()
    app.config["TESTING"] = True

    # Patch pipeline and user resolution
    import webapp.routes as routes
    monkeypatch.setattr(
        routes,
        "_resolve_api_user",
        lambda token: "testuser" if token == "valid-token" else None,
    )

    import app.commit_pipeline as cp
    monkeypatch.setattr(cp, "MemoryCommitPipeline", lambda: _FakePipeline())

    return app.test_client()


# ── tests ─────────────────────────────────────────────────────────────────────

def test_ingest_missing_auth(monkeypatch):
    client = _make_app(monkeypatch)
    r = client.post("/api/ingest", json={"content": "hello"})
    assert r.status_code == 401


def test_ingest_bad_token(monkeypatch):
    client = _make_app(monkeypatch)
    r = client.post(
        "/api/ingest",
        json={"content": "hello"},
        headers={"Authorization": "Bearer bad-token"},
    )
    assert r.status_code == 401


def test_ingest_empty_content(monkeypatch):
    client = _make_app(monkeypatch)
    r = client.post(
        "/api/ingest",
        json={"content": ""},
        headers={"Authorization": "Bearer valid-token"},
    )
    assert r.status_code == 400


def test_ingest_success_returns_atom_and_signal_ids(monkeypatch):
    client = _make_app(monkeypatch)
    r = client.post(
        "/api/ingest",
        json={"content": "This is a test fact about LLMs."},
        headers={"Authorization": "Bearer valid-token"},
    )
    assert r.status_code == 200
    data = json.loads(r.data)
    assert data["stored"] is True
    assert data["atom_id"] is not None
    assert data["signal_id"] is not None
    # Confirm no internal vocabulary leaked into the response
    assert "CommitDecision" not in json.dumps(data)
    assert "memory_atom_id" not in data


def test_ingest_response_has_no_internal_vocab(monkeypatch):
    client = _make_app(monkeypatch)
    r = client.post(
        "/api/ingest",
        json={"content": "Another test atom.", "memory_type": "fact"},
        headers={"Authorization": "Bearer valid-token"},
    )
    body = json.dumps(json.loads(r.data))
    for internal in ("memory_atom", "candidate", "lifecycle", "reconcil", "critic"):
        assert internal not in body, f"internal term '{internal}' leaked into API response"
