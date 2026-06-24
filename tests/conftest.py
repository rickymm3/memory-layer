"""Shared fixtures for memory-layer tests."""
from __future__ import annotations

import math
import os
import tempfile
from typing import Generator

import pytest

from app.sqlite_store import SQLiteStore


class _FakeEmbedder:
    """Deterministic fake embedder — identical text → identical vector."""

    DIM = 8

    def embed_text(self, text: str) -> list[float]:
        seed = hash(text) % (2**31)
        vec = [math.sin(seed + i) for i in range(self.DIM)]
        mag = math.sqrt(sum(x * x for x in vec)) or 1.0
        return [x / mag for x in vec]


@pytest.fixture
def store(tmp_path) -> Generator[SQLiteStore, None, None]:
    """A fresh SQLiteStore backed by a temp file with a fake embedder."""
    db = tmp_path / "test_memory.db"
    s = SQLiteStore(str(db), ollama_client=_FakeEmbedder())
    s.init_db()
    yield s
