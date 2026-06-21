"""SQLite backend for memory-layer — zero-config local storage.

Mirrors the MemoryStore interface. Uses stdlib sqlite3 only.
Embeddings stored as JSON; cosine similarity computed in Python.
All timestamps stored as ISO-8601 strings; UUIDs generated in Python.
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import get_config
from app.llm_provider import LLMProvider, get_embedding_client

REVISABLE_TYPES: frozenset[str] = frozenset({
    "opinion", "preference", "decision", "lesson", "belief",
})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_atoms (
    id TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    context_summary TEXT,
    memory_type TEXT NOT NULL DEFAULT 'fact',
    scope TEXT,
    confidence REAL NOT NULL DEFAULT 0.800,
    importance REAL NOT NULL DEFAULT 0.500,
    embedding_model TEXT NOT NULL DEFAULT 'unknown',
    embedding TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    support_weight REAL NOT NULL DEFAULT 0.0,
    opposition_weight REAL NOT NULL DEFAULT 0.0,
    disagreement_score REAL NOT NULL DEFAULT 0.0,
    last_recomputed_at TEXT,
    lifecycle_status TEXT NOT NULL DEFAULT 'active',
    superseded_by_atom_id TEXT,
    lifecycle_reason TEXT,
    retrieval_priority REAL NOT NULL DEFAULT 1.0,
    lifecycle_updated_at TEXT,
    source_type TEXT,
    source_url TEXT
);

CREATE TABLE IF NOT EXISTS memory_signals (
    id TEXT PRIMARY KEY,
    memory_atom_id TEXT REFERENCES memory_atoms(id) ON DELETE SET NULL,
    parent_signal_id TEXT,
    source_key TEXT NOT NULL DEFAULT 'local_user',
    source_type TEXT NOT NULL DEFAULT 'local',
    source_id TEXT,
    content TEXT NOT NULL,
    context_summary TEXT,
    memory_type TEXT NOT NULL,
    scope TEXT,
    subject TEXT,
    stance TEXT,
    relationship TEXT,
    certainty REAL,
    intensity REAL,
    confidence REAL,
    importance REAL,
    raw_input TEXT,
    reconciliation_reason TEXT,
    metadata TEXT,
    created_at TEXT NOT NULL,
    task_run_id TEXT
);

CREATE TABLE IF NOT EXISTS memory_proposals (
    id TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    context_summary TEXT,
    memory_type TEXT NOT NULL DEFAULT 'fact',
    scope TEXT,
    confidence REAL NOT NULL DEFAULT 0.800,
    importance REAL NOT NULL DEFAULT 0.500,
    relationship TEXT,
    reconciliation_reason TEXT,
    matched_memory_ids TEXT,
    status TEXT NOT NULL DEFAULT 'pending_review',
    approval_token TEXT,
    token_expires_at TEXT,
    reviewed_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_runs (
    id TEXT PRIMARY KEY,
    scope TEXT,
    task_description TEXT,
    model_used TEXT,
    files_changed TEXT,
    outcome TEXT,
    lessons_stored INTEGER,
    notes TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memory_commit_traces (
    id TEXT PRIMARY KEY,
    candidate_text TEXT,
    final_memory_text TEXT,
    decision TEXT,
    write_action TEXT,
    memory_type TEXT,
    scope TEXT,
    confidence REAL,
    lifecycle_action TEXT,
    duplicate_atom_ids TEXT,
    reinforces_atom_ids TEXT,
    refines_atom_ids TEXT,
    supersedes_atom_ids TEXT,
    conflicts_with_atom_ids TEXT,
    committed_atom_id TEXT,
    proposal_id TEXT,
    critic_notes TEXT,
    rejection_reason TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runtime_context_traces (
    id TEXT PRIMARY KEY,
    task_summary TEXT,
    retrieved_atom_ids TEXT,
    used_atom_ids TEXT,
    ignored_atom_ids TEXT,
    context_status TEXT,
    confidence REAL,
    issues TEXT,
    required_actions TEXT,
    final_action TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runtime_response_traces (
    id TEXT PRIMARY KEY,
    user_message TEXT,
    draft_answer TEXT,
    final_answer TEXT,
    verdict TEXT,
    action_followed TEXT,
    overstatement_risk TEXT,
    issues TEXT,
    commit_candidates TEXT,
    reasoning TEXT,
    context_trace_id TEXT,
    gap_status TEXT,
    gap_searches INTEGER,
    gap_clarifying_question TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS belief_revision_log (
    id TEXT PRIMARY KEY,
    atom_id TEXT,
    prior_atom_id TEXT,
    prior_content TEXT,
    new_content TEXT,
    prior_confidence REAL,
    new_confidence REAL,
    memory_type TEXT,
    scope TEXT,
    event_type TEXT,
    revision_reason TEXT,
    source_key TEXT,
    revised_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memory_atom_relations (
    id TEXT PRIMARY KEY,
    atom_a_id TEXT NOT NULL REFERENCES memory_atoms(id) ON DELETE CASCADE,
    atom_b_id TEXT NOT NULL REFERENCES memory_atoms(id) ON DELETE CASCADE,
    relation_type TEXT NOT NULL DEFAULT 'related',
    confidence REAL NOT NULL DEFAULT 0.8,
    created_at TEXT NOT NULL,
    source_key TEXT NOT NULL DEFAULT 'local_user'
);
CREATE INDEX IF NOT EXISTS idx_atom_relations_a ON memory_atom_relations(atom_a_id);
CREATE INDEX IF NOT EXISTS idx_atom_relations_b ON memory_atom_relations(atom_b_id);
"""


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _uid() -> str:
    return str(uuid.uuid4())


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = math.sqrt(sum(x * x for x in a))
    mag_b = math.sqrt(sum(y * y for y in b))
    if mag_a == 0 or mag_b == 0:
        return 0.0
    return dot / (mag_a * mag_b)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


# Atom types where recency matters during retrieval ranking.
_VOLATILE_TYPES: frozenset[str] = frozenset({"opinion", "preference", "lesson", "belief"})

# Half-life (days) for retrieval recency decay on volatile types.
_RETRIEVAL_HALF_LIFE_DAYS: float = 90.0


def _retrieval_recency(created_at: str | datetime | None) -> float:
    """Return a recency multiplier in [0.3, 1.0] for volatile atom ranking.

    Volatile atoms (opinions, preferences) lose ranking weight as they age,
    so fresh beliefs surface above semantically-similar stale ones.
    """
    if not created_at:
        return 1.0
    try:
        if isinstance(created_at, str):
            dt = datetime.fromisoformat(created_at)
        else:
            dt = created_at
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age_days = max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 86400)
        return max(0.3, math.exp(-math.log(2) * age_days / _RETRIEVAL_HALF_LIFE_DAYS))
    except Exception:
        return 1.0


def _mmr_select(
    query_emb: list[float],
    candidates: list[tuple[dict, float, float, list[float]]],
    k: int,
    lambda_: float = 0.6,
) -> list[dict]:
    """Maximal Marginal Relevance selection — pick k diverse, relevant atoms.

    Each iteration picks the candidate that maximises:
        lambda_ * composite_score  -  (1 - lambda_) * max_similarity_to_selected

    lambda_=0.6 weights relevance 60%, diversity 40%, so strong signals still
    surface even when similar — but redundant near-copies are deprioritised.
    """
    if not candidates:
        return []

    selected: list[tuple[dict, float, float, list[float]]] = []
    remaining = list(candidates)

    while len(selected) < k and remaining:
        if not selected:
            best = max(remaining, key=lambda x: x[2])
        else:
            selected_embs = [s[3] for s in selected]
            best, best_score = None, float("-inf")
            for cand in remaining:
                _, _, composite, emb = cand
                max_red = max(_cosine(emb, s_emb) for s_emb in selected_embs)
                mmr = lambda_ * composite - (1 - lambda_) * max_red
                if mmr > best_score:
                    best_score = mmr
                    best = cand
            if best is None:
                break
        selected.append(best)
        remaining.remove(best)

    return [s[0] for s in selected]


def _sl_atom_row_to_dict(row: sqlite3.Row | tuple, similarity: float | None = None) -> dict[str, Any]:
    """Convert a 21-column SELECT * from memory_atoms row to a dict."""
    ds = float(row[12]) if row[12] is not None else 0.0
    d: dict[str, Any] = {
        "id": row[0],
        "content": row[1],
        "context_summary": row[2],
        "memory_type": row[3],
        "scope": row[4],
        "confidence": float(row[5]),
        "importance": float(row[6]),
        "created_at": row[9],
        "support_weight": float(row[10]) if row[10] is not None else 0.0,
        "opposition_weight": float(row[11]) if row[11] is not None else 0.0,
        "disagreement_score": ds,
        "last_recomputed_at": row[13],
        "disagreement_flag": ds >= 0.5,
        "lifecycle_status": row[14] or "active",
        "superseded_by_atom_id": row[15],
        "lifecycle_reason": row[16],
        "retrieval_priority": float(row[17]) if row[17] is not None else 1.0,
        "lifecycle_updated_at": row[18],
    }
    if similarity is not None:
        d["similarity"] = similarity
    return d


class SQLiteStore:
    def __init__(self, db_path: str, ollama_client: LLMProvider | None = None) -> None:
        self._db_path = str(db_path)
        self.config = get_config()
        self.ollama = ollama_client or get_embedding_client()
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn

    def init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    # ── Core write path ───────────────────────────────────────────────────────

    @staticmethod
    def _normalize_text(value: str) -> str:
        return _normalize(value)

    def store_memory(
        self,
        content: str,
        context_summary: str | None = None,
        memory_type: str = "fact",
        scope: str | None = None,
        confidence: float = 0.8,
        importance: float = 0.5,
    ) -> str:
        summary = (context_summary or "").strip() or content
        embedding = self.ollama.embed_text(content)
        atom_id = _uid()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO memory_atoms
                    (id, content, context_summary, memory_type, scope,
                     confidence, importance, embedding_model, embedding, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (atom_id, content, summary, memory_type, scope,
                 confidence, importance, self.config.embedding_model,
                 json.dumps(embedding), _now()),
            )
        return atom_id

    def store_memory_with_signal(
        self,
        content: str,
        context_summary: str | None = None,
        memory_type: str = "fact",
        scope: str | None = None,
        confidence: float = 0.8,
        importance: float = 0.5,
        relationship: str | None = None,
        reconciliation_reason: str | None = None,
        raw_input: str | None = None,
        signal_metadata: dict | None = None,
        source_key: str = "local_user",
        source_type: str = "local",
        task_run_id: str | None = None,
        source_url: str | None = None,
        atom_source_type: str | None = None,
    ) -> tuple[str, str]:
        summary = (context_summary or "").strip() or content
        embedding = self.ollama.embed_text(content)
        metadata_json = json.dumps(signal_metadata) if signal_metadata else None
        _atom_source_type = atom_source_type or source_type
        now = _now()
        atom_id = _uid()
        signal_id = _uid()

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO memory_atoms
                    (id, content, context_summary, memory_type, scope,
                     confidence, importance, embedding_model, embedding,
                     source_type, source_url, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (atom_id, content, summary, memory_type, scope,
                 confidence, importance, self.config.embedding_model,
                 json.dumps(embedding), _atom_source_type, source_url, now),
            )
            conn.execute(
                """
                INSERT INTO memory_signals
                    (id, memory_atom_id, source_key, source_type,
                     content, context_summary, memory_type, scope,
                     relationship, confidence, importance,
                     raw_input, reconciliation_reason, metadata, task_run_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (signal_id, atom_id, source_key, source_type,
                 content, context_summary, memory_type, scope,
                 relationship, confidence, importance,
                 raw_input, reconciliation_reason, metadata_json, task_run_id, now),
            )

        self.recompute_atom_weights(atom_id)
        return atom_id, signal_id

    def recompute_atom_weights(self, atom_id: str) -> dict[str, Any] | None:
        from app.signal_aggregator import compute_atom_weights, compute_source_trust

        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, memory_type FROM memory_atoms WHERE id = ?;", (atom_id,)
            ).fetchone()
            if row is None:
                return None
            atom_memory_type = row[1]

            signal_rows = conn.execute(
                "SELECT relationship, confidence, source_key, created_at FROM memory_signals WHERE memory_atom_id = ?;",
                (atom_id,),
            ).fetchall()

            source_agg = conn.execute(
                """
                SELECT source_key, COUNT(*) AS total,
                       SUM(CASE WHEN relationship IN ('conflict','opinion_change') THEN 1 ELSE 0 END) AS conflicts
                FROM memory_signals GROUP BY source_key;
                """
            ).fetchall()

        source_stats = [{"source_key": r[0], "total": r[1], "conflicts": r[2]} for r in source_agg]
        source_trust = compute_source_trust(source_stats)
        signals = [{"relationship": r[0], "confidence": r[1], "source_key": r[2], "created_at": r[3]}
                   for r in signal_rows]
        weights = compute_atom_weights(signals, memory_type=atom_memory_type, source_trust=source_trust)

        now = _now()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE memory_atoms
                SET support_weight=?, opposition_weight=?, disagreement_score=?,
                    confidence=?, retrieval_priority=?, last_recomputed_at=?
                WHERE id=?;
                """,
                (weights["support_weight"], weights["opposition_weight"],
                 weights["disagreement_score"], weights["confidence"],
                 weights.get("retrieval_priority", 1.0), now, atom_id),
            )
            row = conn.execute(
                """
                SELECT id, content, context_summary, memory_type, scope,
                       confidence, importance, support_weight, opposition_weight,
                       disagreement_score, last_recomputed_at, created_at
                FROM memory_atoms WHERE id=?;
                """,
                (atom_id,),
            ).fetchone()

        if row is None:
            return None
        return {
            "id": row[0], "content": row[1], "context_summary": row[2],
            "memory_type": row[3], "scope": row[4],
            "confidence": float(row[5]), "importance": float(row[6]),
            "support_weight": float(row[7]), "opposition_weight": float(row[8]),
            "disagreement_score": float(row[9]),
            "last_recomputed_at": row[10],
            "created_at": row[11],
        }

    def find_near_duplicates(
        self, content: str, threshold: float = 0.93, limit: int = 3
    ) -> list[dict[str, Any]]:
        embedding = self.ollama.embed_text(content)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, content, context_summary, memory_type, scope, confidence, importance, embedding "
                "FROM memory_atoms;"
            ).fetchall()

        results = []
        for row in rows:
            try:
                stored = json.loads(row[7])
            except Exception:
                continue
            sim = _cosine(embedding, stored)
            if sim >= threshold:
                results.append({
                    "id": row[0], "content": row[1], "context_summary": row[2],
                    "memory_type": row[3], "scope": row[4],
                    "confidence": float(row[5]), "importance": float(row[6]),
                    "similarity": sim,
                })
        results.sort(key=lambda x: x["similarity"], reverse=True)
        return results[:limit]

    def store_memory_if_not_duplicate(
        self,
        content: str,
        context_summary: str | None = None,
        memory_type: str = "fact",
        scope: str | None = None,
        confidence: float = 0.8,
        importance: float = 0.5,
        dupe_threshold: float = 0.93,
        dupe_limit: int = 3,
    ) -> tuple[str | None, list[dict[str, Any]]]:
        duplicates = self.find_near_duplicates(content=content, threshold=dupe_threshold, limit=dupe_limit)
        if duplicates:
            incoming_summary = (context_summary or "").strip() or None
            if incoming_summary:
                top = duplicates[0]
                if not (isinstance(top.get("context_summary"), str) and top["context_summary"].strip()):
                    with self._connect() as conn:
                        conn.execute(
                            "UPDATE memory_atoms SET context_summary=? WHERE id=? AND (context_summary IS NULL OR trim(context_summary)='');",
                            (incoming_summary, top["id"]),
                        )
                    top["context_summary"] = incoming_summary
                    top["context_summary_updated"] = True
            return None, duplicates

        memory_id = self.store_memory(
            content=content, context_summary=context_summary, memory_type=memory_type,
            scope=scope, confidence=confidence, importance=importance,
        )
        return memory_id, []

    def find_duplicate_candidate(
        self, content: str, memory_type: str, scope: str | None,
        dupe_threshold: float = 0.93, dupe_limit: int = 3,
    ) -> dict[str, Any] | None:
        exact = self.find_exact_content_match(content)
        if exact:
            exact["match_type"] = "exact"
            return exact
        for match in self.find_near_duplicates(content=content, threshold=dupe_threshold, limit=dupe_limit):
            if match.get("memory_type") == memory_type and (match.get("scope") or None) == (scope or None):
                match["match_type"] = "near"
                return match
        return None

    def find_exact_content_match(self, content: str) -> dict[str, Any] | None:
        normalized = _normalize(content)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, content, context_summary, memory_type, scope, confidence, importance, created_at "
                "FROM memory_atoms ORDER BY created_at DESC;"
            ).fetchall()
        for row in rows:
            if _normalize(row[1]) == normalized:
                return {
                    "id": row[0], "content": row[1], "context_summary": row[2],
                    "memory_type": row[3], "scope": row[4],
                    "confidence": float(row[5]), "importance": float(row[6]),
                    "created_at": row[7], "similarity": 1.0,
                }
        return None

    def get_memory(self, memory_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, memory_type, scope, content, context_summary, confidence, importance, "
                "embedding_model, created_at FROM memory_atoms WHERE id=?;",
                (memory_id,),
            ).fetchone()
        if not row:
            return None
        return {
            "id": row[0], "memory_type": row[1], "scope": row[2], "content": row[3],
            "context_summary": row[4], "confidence": float(row[5]), "importance": float(row[6]),
            "embedding_model": row[7], "created_at": row[8],
        }

    def list_memories(
        self, scope: str | None = None, memory_type: str | None = None, limit: int = 20
    ) -> list[dict[str, Any]]:
        conditions: list[str] = []
        params: list[Any] = []
        if scope:
            conditions.append("scope=?")
            params.append(scope)
        if memory_type:
            conditions.append("memory_type=?")
            params.append(memory_type)
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        params.append(limit)

        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT id, memory_type, scope, content, context_summary, created_at "
                f"FROM memory_atoms {where} ORDER BY created_at DESC LIMIT ?;",
                tuple(params),
            ).fetchall()
        return [{"id": r[0], "memory_type": r[1], "scope": r[2], "content": r[3],
                 "context_summary": r[4], "created_at": r[5]} for r in rows]

    def retrieve_memories(
        self,
        query: str,
        limit: int = 5,
        min_similarity: float | None = None,
        scope_filter: str | None = None,
        scope_filters: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        all_scopes: set[str] | None = None
        if scope_filters:
            all_scopes = set(scope_filters)
        if scope_filter:
            all_scopes = (all_scopes or set()) | {scope_filter}

        embedding = self.ollama.embed_text(query)
        threshold = min_similarity if min_similarity is not None else self.config.memory_retrieval_threshold

        # Pre-filter by scope in SQL when possible to reduce Python-side work.
        if all_scopes:
            placeholders = ",".join("?" * len(all_scopes))
            sql = (
                "SELECT id, content, context_summary, memory_type, scope, confidence, importance, "
                "embedding, created_at, lifecycle_status, support_weight, opposition_weight, disagreement_score "
                "FROM memory_atoms "
                "WHERE (lifecycle_status IS NULL OR lifecycle_status NOT IN ('superseded','deprecated','archived')) "
                f"AND (scope IN ({placeholders}) OR scope IS NULL OR scope = 'global');"
            )
            params: tuple = tuple(all_scopes)
        else:
            sql = (
                "SELECT id, content, context_summary, memory_type, scope, confidence, importance, "
                "embedding, created_at, lifecycle_status, support_weight, opposition_weight, disagreement_score "
                "FROM memory_atoms "
                "WHERE lifecycle_status IS NULL OR lifecycle_status NOT IN ('superseded','deprecated','archived');"
            )
            params = ()

        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()

        # Score each candidate with a trust-ranked composite formula.
        # Embeddings are kept in the candidate tuple for MMR diversity selection.
        candidates: list[tuple[dict, float, float, list[float]]] = []
        for row in rows:
            try:
                stored_emb = json.loads(row[7])
            except Exception:
                continue
            sim = _cosine(embedding, stored_emb)
            if sim < threshold:
                continue

            conf = float(row[5]) if row[5] is not None else 0.5
            ds = float(row[12]) if row[12] is not None else 0.0

            # Composite: similarity + confidence bonus - disagreement penalty.
            # High-confidence, uncontested atoms rank above semantically-similar
            # but contested or low-evidence ones.
            composite = sim * 0.60 + conf * 0.25 - ds * 0.15

            # Volatile types (opinion, preference, lesson, belief) decay so that
            # stale beliefs don't compete equally with fresh ones.
            if row[3] in _VOLATILE_TYPES:
                composite *= _retrieval_recency(row[8])

            atom_dict: dict[str, Any] = {
                "id": row[0], "content": row[1], "context_summary": row[2],
                "memory_type": row[3], "scope": row[4],
                "confidence": conf,
                "importance": float(row[6]) if row[6] is not None else 0.5,
                "similarity": sim,
                "composite_score": round(composite, 4),
                "created_at": row[8],
                "lifecycle_status": row[9] or "active",
                "support_weight": float(row[10]) if row[10] is not None else 0.0,
                "opposition_weight": float(row[11]) if row[11] is not None else 0.0,
                "disagreement_score": ds,
                "disagreement_flag": ds >= 0.5,
            }
            candidates.append((atom_dict, sim, composite, stored_emb))

        if not candidates:
            return []

        # Sort by composite, then apply MMR on a 3× pool so the final k atoms
        # are both relevant and diverse — no more 5 redundant near-copies.
        candidates.sort(key=lambda x: x[2], reverse=True)
        pool = candidates[: limit * 3]
        return _mmr_select(query_emb=embedding, candidates=pool, k=limit)

    # ── Signal / proposal writes ──────────────────────────────────────────────

    def add_signal_to_atom(
        self, atom_id: str, content: str, relationship: str = "reinforcement",
        context_summary: str | None = None, memory_type: str = "fact",
        scope: str | None = None, confidence: float = 0.8, importance: float = 0.5,
        source_key: str = "local_user", source_type: str = "local",
        reconciliation_reason: str | None = None,
    ) -> str:
        summary = (context_summary or "").strip() or content
        signal_id = _uid()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO memory_signals
                    (id, memory_atom_id, source_key, source_type,
                     content, context_summary, memory_type, scope,
                     relationship, confidence, importance, reconciliation_reason, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (signal_id, atom_id, source_key, source_type,
                 content, summary, memory_type, scope,
                 relationship, confidence, importance, reconciliation_reason, _now()),
            )
        self.recompute_atom_weights(atom_id)
        return signal_id

    def store_proposal(
        self, content: str, memory_type: str, relationship: str,
        context_summary: str | None = None, scope: str | None = None,
        confidence: float = 0.8, importance: float = 0.5,
        reconciliation_reason: str | None = None,
        matched_memory_ids: list[str] | None = None,
    ) -> str:
        summary = (context_summary or "").strip() or content
        matched_json = json.dumps(matched_memory_ids) if matched_memory_ids else None
        proposal_id = _uid()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO memory_proposals
                    (id, content, context_summary, memory_type, scope,
                     confidence, importance, relationship, reconciliation_reason,
                     matched_memory_ids, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (proposal_id, content.strip(), summary, memory_type.strip(), scope,
                 max(0.0, min(1.0, float(confidence))),
                 max(0.0, min(1.0, float(importance))),
                 relationship, reconciliation_reason, matched_json, _now()),
            )
        return proposal_id

    def store_commit_trace(self, trace: dict[str, Any]) -> str:
        trace_id = _uid()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO memory_commit_traces
                    (id, candidate_text, final_memory_text, decision, write_action,
                     memory_type, scope, confidence, lifecycle_action,
                     duplicate_atom_ids, reinforces_atom_ids, refines_atom_ids,
                     supersedes_atom_ids, conflicts_with_atom_ids,
                     committed_atom_id, proposal_id, critic_notes, rejection_reason, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (trace_id,
                 trace.get("candidate_text", ""), trace.get("final_memory_text"),
                 trace.get("decision", "reject"), trace.get("write_action"),
                 trace.get("memory_type"), trace.get("scope"), trace.get("confidence"),
                 trace.get("lifecycle_action"),
                 json.dumps(trace.get("duplicate_atom_ids") or []),
                 json.dumps(trace.get("reinforces_atom_ids") or []),
                 json.dumps(trace.get("refines_atom_ids") or []),
                 json.dumps(trace.get("supersedes_atom_ids") or []),
                 json.dumps(trace.get("conflicts_with_atom_ids") or []),
                 trace.get("committed_atom_id") or None,
                 trace.get("proposal_id") or None,
                 json.dumps(trace.get("critic_notes") or []),
                 trace.get("rejection_reason"), _now()),
            )
        return trace_id

    def log_belief_revision(
        self, atom_id: str, new_content: str, new_confidence: float,
        memory_type: str, event_type: str, scope: str | None = None,
        prior_atom_id: str | None = None, prior_content: str | None = None,
        prior_confidence: float | None = None, revision_reason: str | None = None,
        source_key: str = "local_user",
    ) -> str:
        log_id = _uid()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO belief_revision_log
                    (id, atom_id, prior_atom_id, prior_content, new_content,
                     prior_confidence, new_confidence, memory_type, scope,
                     event_type, revision_reason, source_key, revised_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (log_id, atom_id, prior_atom_id, prior_content, new_content,
                 prior_confidence, new_confidence, memory_type, scope,
                 event_type, revision_reason, source_key, _now()),
            )
        return log_id

    def get_belief_detail(self, atom_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, content, context_summary, memory_type, scope,
                       confidence, importance, support_weight, opposition_weight,
                       disagreement_score, lifecycle_status, lifecycle_reason,
                       superseded_by_atom_id, created_at, last_recomputed_at
                FROM memory_atoms WHERE id=?;
                """,
                (atom_id,),
            ).fetchone()
            if row is None:
                return None

            ds = float(row[9]) if row[9] is not None else 0.0
            atom = {
                "id": row[0], "content": row[1], "context_summary": row[2],
                "memory_type": row[3], "scope": row[4],
                "confidence": float(row[5]), "importance": float(row[6]),
                "support_weight": float(row[7]) if row[7] is not None else 0.0,
                "opposition_weight": float(row[8]) if row[8] is not None else 0.0,
                "disagreement_score": ds,
                "lifecycle_status": row[10] or "active",
                "lifecycle_reason": row[11],
                "superseded_by_atom_id": row[12],
                "created_at": row[13],
                "last_recomputed_at": row[14],
            }

            sig_rows = conn.execute(
                "SELECT id, content, relationship, confidence, source_key, reconciliation_reason, created_at "
                "FROM memory_signals WHERE memory_atom_id=? ORDER BY created_at ASC;",
                (atom_id,),
            ).fetchall()

            rev_rows = conn.execute(
                "SELECT id, prior_atom_id, prior_content, new_content, prior_confidence, new_confidence, "
                "event_type, revision_reason, source_key, revised_at "
                "FROM belief_revision_log WHERE atom_id=? ORDER BY revised_at ASC;",
                (atom_id,),
            ).fetchall()

        supporting, opposing = [], []
        for sr in sig_rows:
            sig = {
                "id": sr[0], "content": sr[1], "relationship": sr[2],
                "confidence": float(sr[3]) if sr[3] is not None else None,
                "source_key": sr[4], "reason": sr[5], "created_at": sr[6],
            }
            rel = (sr[2] or "").lower()
            if rel in ("conflict", "opposition", "opinion_change"):
                opposing.append(sig)
            else:
                supporting.append(sig)

        revision_history = [
            {
                "id": r[0], "prior_atom_id": r[1], "prior_content": r[2],
                "new_content": r[3],
                "prior_confidence": float(r[4]) if r[4] is not None else None,
                "new_confidence": float(r[5]),
                "event_type": r[6], "revision_reason": r[7], "source_key": r[8], "revised_at": r[9],
            }
            for r in rev_rows
        ]

        return {
            "atom": atom,
            "signals": {"supporting": supporting, "opposing": opposing},
            "revision_history": revision_history,
        }

    # ── Shared interface methods ──────────────────────────────────────────────

    def health_stats(self) -> dict[str, Any]:
        with self._connect() as conn:
            count = conn.execute("SELECT COUNT(*) FROM memory_atoms;").fetchone()[0]
            scopes = [r[0] for r in conn.execute("SELECT DISTINCT scope FROM memory_atoms ORDER BY scope;").fetchall()]
        return {"atom_count": int(count), "available_scopes": scopes, "backend": "sqlite"}

    def health_report(self) -> dict[str, Any]:
        """Return a detailed health report for the dashboard /health page."""
        with self._connect() as conn:
            lifecycle_rows = conn.execute(
                "SELECT COALESCE(lifecycle_status,'active'), COUNT(*) FROM memory_atoms GROUP BY lifecycle_status;"
            ).fetchall()

            active_stats = conn.execute(
                """
                SELECT
                    COUNT(*) AS total_active,
                    SUM(CASE WHEN disagreement_score >= 0.4 THEN 1 ELSE 0 END) AS contested,
                    SUM(CASE WHEN support_weight = 0 THEN 1 ELSE 0 END) AS orphans,
                    AVG(confidence) AS avg_confidence,
                    AVG(disagreement_score) AS avg_disagreement
                FROM memory_atoms
                WHERE lifecycle_status = 'active' OR lifecycle_status IS NULL;
                """
            ).fetchone()

            scope_rows = conn.execute(
                """
                SELECT COALESCE(scope,'(none)'), COUNT(*)
                FROM memory_atoms
                WHERE lifecycle_status = 'active' OR lifecycle_status IS NULL
                GROUP BY scope ORDER BY COUNT(*) DESC LIMIT 12;
                """
            ).fetchall()

            type_rows = conn.execute(
                """
                SELECT memory_type, COUNT(*)
                FROM memory_atoms
                WHERE lifecycle_status = 'active' OR lifecycle_status IS NULL
                GROUP BY memory_type ORDER BY COUNT(*) DESC;
                """
            ).fetchall()

            contested_rows = conn.execute(
                """
                SELECT id, content, disagreement_score, confidence, COALESCE(scope,'(none)')
                FROM memory_atoms
                WHERE (lifecycle_status = 'active' OR lifecycle_status IS NULL)
                  AND disagreement_score >= 0.4
                ORDER BY disagreement_score DESC LIMIT 5;
                """
            ).fetchall()

            signal_stats = conn.execute(
                """
                SELECT
                    COUNT(DISTINCT memory_atom_id) AS atoms_with_signals,
                    COUNT(*) AS total_signals
                FROM memory_signals;
                """
            ).fetchone()

            recent_signals = conn.execute(
                """
                SELECT date(created_at) AS day, COUNT(*)
                FROM memory_signals
                WHERE created_at >= datetime('now', '-14 days')
                GROUP BY day ORDER BY day DESC;
                """
            ).fetchall()

            graph_stats = conn.execute(
                """
                SELECT COUNT(*) AS total_relations,
                       COUNT(DISTINCT atom_a_id) + COUNT(DISTINCT atom_b_id) AS atoms_in_graph
                FROM memory_atom_relations;
                """
            ).fetchone()

            relation_type_rows = conn.execute(
                "SELECT relation_type, COUNT(*) FROM memory_atom_relations GROUP BY relation_type;"
            ).fetchall()

        lifecycle = {row[0]: int(row[1]) for row in lifecycle_rows}
        active = int(active_stats[0] or 0)

        def pct(n: int) -> float:
            return round(n / active * 100, 1) if active else 0.0

        return {
            "backend": "sqlite",
            "total_atoms": sum(lifecycle.values()),
            "active_atoms": active,
            "lifecycle": lifecycle,
            "conflict_rate": pct(int(active_stats[1] or 0)),
            "orphan_rate": pct(int(active_stats[2] or 0)),
            "contested_count": int(active_stats[1] or 0),
            "orphan_count": int(active_stats[2] or 0),
            "avg_confidence": round(float(active_stats[3] or 0), 3),
            "avg_disagreement": round(float(active_stats[4] or 0), 3),
            "scope_distribution": {row[0]: int(row[1]) for row in scope_rows},
            "type_distribution": {row[0]: int(row[1]) for row in type_rows},
            "top_contested": [
                {
                    "id": r[0],
                    "content": r[1][:90] + "…" if len(r[1]) > 90 else r[1],
                    "disagreement_score": round(float(r[2]), 3),
                    "confidence": round(float(r[3]), 3),
                    "scope": r[4],
                }
                for r in contested_rows
            ],
            "signal_coverage": {
                "atoms_with_signals": int(signal_stats[0] or 0),
                "total_signals": int(signal_stats[1] or 0),
                "coverage_pct": pct(int(signal_stats[0] or 0)),
            },
            "signal_activity_14d": {row[0]: int(row[1]) for row in recent_signals},
            "graph": {
                "total_relations": int(graph_stats[0] or 0),
                "atoms_in_graph": int(graph_stats[1] or 0),
                "relation_types": {row[0]: int(row[1]) for row in relation_type_rows},
            },
        }

    def list_recent(self, limit: int = 10, scope: str | None = None) -> list[dict[str, Any]]:
        params: list[Any] = []
        scope_clause = ""
        if scope is not None:
            scope_clause = "AND scope=?"
            params.append(scope)
        params.append(limit)

        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT id, content, context_summary, memory_type, scope,
                       confidence, importance, created_at, support_weight,
                       opposition_weight, disagreement_score, last_recomputed_at,
                       lifecycle_status, superseded_by_atom_id, lifecycle_reason,
                       retrieval_priority, lifecycle_updated_at
                FROM memory_atoms
                WHERE lifecycle_status != 'archived' {scope_clause}
                ORDER BY created_at DESC LIMIT ?;
                """,
                tuple(params),
            ).fetchall()

        results = []
        for r in rows:
            ds = float(r[10]) if r[10] is not None else 0.0
            results.append({
                "id": r[0], "content": r[1], "context_summary": r[2],
                "memory_type": r[3], "scope": r[4],
                "confidence": float(r[5]), "importance": float(r[6]),
                "created_at": r[7],
                "support_weight": float(r[8]) if r[8] is not None else 0.0,
                "opposition_weight": float(r[9]) if r[9] is not None else 0.0,
                "disagreement_score": ds,
                "last_recomputed_at": r[11],
                "disagreement_flag": ds >= 0.5,
                "lifecycle_status": r[12] or "active",
                "superseded_by_atom_id": r[13],
                "lifecycle_reason": r[14],
                "retrieval_priority": float(r[15]) if r[15] is not None else 1.0,
                "lifecycle_updated_at": r[16],
            })
        return results

    def project_context_atoms(
        self, scope: str, limit: int = 10,
        min_importance: float = 0.6, min_confidence: float = 0.7,
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, content, context_summary, memory_type, scope,
                       confidence, importance, created_at, support_weight,
                       opposition_weight, disagreement_score, last_recomputed_at,
                       lifecycle_status, superseded_by_atom_id, lifecycle_reason,
                       retrieval_priority, lifecycle_updated_at
                FROM memory_atoms
                WHERE scope=? AND importance>=? AND confidence>=? AND lifecycle_status='active'
                ORDER BY importance DESC, confidence DESC, created_at DESC LIMIT ?;
                """,
                (scope, min_importance, min_confidence, limit),
            ).fetchall()
        results = []
        for r in rows:
            ds = float(r[10]) if r[10] is not None else 0.0
            results.append({
                "id": r[0], "content": r[1], "context_summary": r[2],
                "memory_type": r[3], "scope": r[4],
                "confidence": float(r[5]), "importance": float(r[6]),
                "created_at": r[7],
                "support_weight": float(r[8]) if r[8] is not None else 0.0,
                "opposition_weight": float(r[9]) if r[9] is not None else 0.0,
                "disagreement_score": ds,
                "last_recomputed_at": r[11],
                "disagreement_flag": ds >= 0.5,
                "lifecycle_status": r[12] or "active",
                "superseded_by_atom_id": r[13],
                "lifecycle_reason": r[14],
                "retrieval_priority": float(r[15]) if r[15] is not None else 1.0,
                "lifecycle_updated_at": r[16],
            })
        return results

    def get_active_atoms_by_scope(self, scope: str, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, content, context_summary, memory_type, scope,
                       confidence, importance, created_at, support_weight,
                       opposition_weight, disagreement_score, last_recomputed_at,
                       lifecycle_status, superseded_by_atom_id, lifecycle_reason,
                       retrieval_priority, lifecycle_updated_at
                FROM memory_atoms
                WHERE scope=? AND lifecycle_status='active'
                ORDER BY importance DESC, confidence DESC, created_at DESC LIMIT ?;
                """,
                (scope, limit),
            ).fetchall()
        results = []
        for r in rows:
            ds = float(r[10]) if r[10] is not None else 0.0
            results.append({
                "id": r[0], "content": r[1], "context_summary": r[2],
                "memory_type": r[3], "scope": r[4],
                "confidence": float(r[5]), "importance": float(r[6]),
                "created_at": r[7],
                "support_weight": float(r[8]) if r[8] is not None else 0.0,
                "opposition_weight": float(r[9]) if r[9] is not None else 0.0,
                "disagreement_score": ds,
                "last_recomputed_at": r[11],
                "disagreement_flag": ds >= 0.5,
                "lifecycle_status": r[12] or "active",
                "superseded_by_atom_id": r[13],
                "lifecycle_reason": r[14],
                "retrieval_priority": float(r[15]) if r[15] is not None else 1.0,
                "lifecycle_updated_at": r[16],
            })
        return results

    def get_stale_atoms(
        self,
        days_threshold: int = 90,
        min_disagreement: float = 0.4,
        scope: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Return active atoms that are stale: old+under-supported, contested, or volatile+aged.

        Three staleness signals:
        - Old (> days_threshold days) with low support_weight (< 0.5) — nobody reinforced them.
        - High disagreement (>= min_disagreement) — contested facts that need review.
        - Volatile type (opinion/preference/lesson/belief) older than 30 days — may have drifted.
        """
        scope_clause = "AND scope = ?" if scope else ""
        scope_param: tuple = (scope,) if scope else ()

        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT id, content, memory_type, scope, confidence,
                       support_weight, disagreement_score, created_at
                FROM memory_atoms
                WHERE lifecycle_status = 'active'
                  {scope_clause}
                  AND (
                    (julianday('now') - julianday(created_at) > ? AND support_weight < 0.5)
                    OR disagreement_score >= ?
                    OR (memory_type IN ('opinion','preference','lesson','belief')
                        AND julianday('now') - julianday(created_at) > 30)
                  )
                ORDER BY disagreement_score DESC, support_weight ASC, created_at ASC
                LIMIT ?;
                """,
                (*scope_param, days_threshold, min_disagreement, limit),
            ).fetchall()

        results: list[dict[str, Any]] = []
        for row in rows:
            ds = float(row[6]) if row[6] is not None else 0.0
            sw = float(row[5]) if row[5] is not None else 0.0
            mt = row[2] or "fact"
            age_days: int | None = None
            try:
                created = datetime.fromisoformat(row[7])
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                age_days = int((datetime.now(timezone.utc) - created).total_seconds() / 86400)
            except Exception:
                pass

            reasons: list[str] = []
            if ds >= min_disagreement:
                reasons.append(f"contested (disagreement_score={ds:.2f})")
            if age_days is not None and age_days > days_threshold and sw < 0.5:
                reasons.append(f"old ({age_days}d) with low support ({sw:.2f})")
            if mt in ("opinion", "preference", "lesson", "belief") and age_days is not None and age_days > 30:
                reasons.append(f"volatile type '{mt}' ({age_days}d old)")

            results.append({
                "id": row[0],
                "content": row[1],
                "memory_type": mt,
                "scope": row[3],
                "confidence": float(row[4]) if row[4] is not None else 0.5,
                "support_weight": sw,
                "disagreement_score": ds,
                "created_at": row[7],
                "age_days": age_days,
                "staleness_reasons": reasons,
            })
        return results

    def find_near_duplicate_pairs(
        self,
        similarity_threshold: float = 0.90,
        scope: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Find pairs of active atoms with cosine similarity above threshold.

        Caps at 500 atoms for performance (O(n²) pairwise comparison).
        Returns pairs sorted by similarity descending — top candidates for consolidation.
        """
        scope_clause = "AND scope = ?" if scope else ""
        scope_param: tuple = (scope,) if scope else ()

        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT id, content, memory_type, scope, confidence, embedding, created_at
                FROM memory_atoms
                WHERE lifecycle_status = 'active'
                  {scope_clause}
                ORDER BY created_at DESC
                LIMIT 500;
                """,
                scope_param,
            ).fetchall()

        atoms: list[dict[str, Any]] = []
        for row in rows:
            try:
                emb = json.loads(row[5])
            except Exception:
                continue
            atoms.append({
                "id": row[0], "content": row[1], "memory_type": row[2] or "fact",
                "scope": row[3], "confidence": float(row[4]) if row[4] is not None else 0.5,
                "created_at": row[6], "_emb": emb,
            })

        pairs: list[dict[str, Any]] = []
        for i in range(len(atoms)):
            for j in range(i + 1, len(atoms)):
                sim = _cosine(atoms[i]["_emb"], atoms[j]["_emb"])
                if sim >= similarity_threshold:
                    pairs.append({
                        "similarity": round(sim, 4),
                        "atom_a": {k: v for k, v in atoms[i].items() if k != "_emb"},
                        "atom_b": {k: v for k, v in atoms[j].items() if k != "_emb"},
                    })

        pairs.sort(key=lambda x: x["similarity"], reverse=True)
        return pairs[:limit]

    # ── Atom relations graph ──────────────────────────────────────────────────

    _VALID_RELATION_TYPES: frozenset[str] = frozenset({
        "supports", "contradicts", "specializes", "generalizes", "related",
    })

    def link_atoms(
        self,
        atom_a_id: str,
        atom_b_id: str,
        relation_type: str = "related",
        confidence: float = 0.8,
        source_key: str = "local_user",
    ) -> str:
        """Create a directed relation from atom_a to atom_b.  Returns the relation id."""
        if relation_type not in self._VALID_RELATION_TYPES:
            raise ValueError(
                f"Invalid relation_type '{relation_type}'. "
                f"Must be one of: {sorted(self._VALID_RELATION_TYPES)}"
            )
        rel_id = _uid()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO memory_atom_relations
                (id, atom_a_id, atom_b_id, relation_type, confidence, created_at, source_key)
                VALUES (?,?,?,?,?,?,?)
                """,
                (rel_id, atom_a_id, atom_b_id, relation_type,
                 float(confidence), _now(), source_key),
            )
        return rel_id

    def get_related_atoms(
        self,
        atom_id: str,
        depth: int = 1,
        relation_types: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Return atoms related to atom_id up to `depth` hops.

        Relations are traversed bidirectionally (a→b and b→a both count).
        """
        depth = max(1, min(depth, 3))
        visited: set[str] = {atom_id}
        frontier: set[str] = {atom_id}
        results: list[dict[str, Any]] = []

        type_filter = ""
        type_params: list[Any] = []
        if relation_types:
            valid = [r for r in relation_types if r in self._VALID_RELATION_TYPES]
            if valid:
                placeholders = ",".join("?" * len(valid))
                type_filter = f"AND r.relation_type IN ({placeholders})"
                type_params = list(valid)

        with self._connect() as conn:
            for _ in range(depth):
                if not frontier:
                    break
                placeholders = ",".join("?" * len(frontier))
                rows = conn.execute(
                    f"""
                    SELECT
                        r.id, r.relation_type, r.confidence, r.source_key,
                        CASE WHEN r.atom_a_id IN ({placeholders})
                             THEN r.atom_b_id ELSE r.atom_a_id END AS neighbor_id,
                        a.content, a.memory_type, a.scope, a.confidence AS atom_confidence,
                        a.lifecycle_status
                    FROM memory_atom_relations r
                    JOIN memory_atoms a ON
                        a.id = CASE WHEN r.atom_a_id IN ({placeholders})
                                    THEN r.atom_b_id ELSE r.atom_a_id END
                    WHERE (r.atom_a_id IN ({placeholders}) OR r.atom_b_id IN ({placeholders}))
                      AND a.lifecycle_status IN ('active', NULL)
                      {type_filter}
                    """,
                    list(frontier) * 4 + type_params,
                ).fetchall()

                next_frontier: set[str] = set()
                for row in rows:
                    neighbor_id = row[4]
                    if neighbor_id not in visited:
                        visited.add(neighbor_id)
                        next_frontier.add(neighbor_id)
                        results.append({
                            "relation_id": row[0],
                            "relation_type": row[1],
                            "relation_confidence": float(row[2]),
                            "source_key": row[3],
                            "id": neighbor_id,
                            "content": row[5],
                            "memory_type": row[6] or "fact",
                            "scope": row[7],
                            "atom_confidence": float(row[8]) if row[8] is not None else 0.5,
                        })
                frontier = next_frontier

        return results

    def list_task_runs_db(
        self, scope: str | None = None, outcome: str | None = None, limit: int = 10
    ) -> list[dict[str, Any]]:
        VALID = {"success", "partial", "failed"}
        conditions: list[str] = []
        params: list[Any] = []
        if scope is not None:
            conditions.append("scope=?")
            params.append(scope)
        if outcome is not None and outcome in VALID:
            conditions.append("outcome=?")
            params.append(outcome)
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        params.append(limit)

        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT id, scope, task_description, model_used, files_changed, outcome, lessons_stored, created_at "
                f"FROM task_runs {where} ORDER BY created_at DESC LIMIT ?;",
                tuple(params),
            ).fetchall()
        return [
            {"id": r[0], "scope": r[1], "task_description": r[2], "model_used": r[3],
             "files_changed": r[4], "outcome": r[5], "lessons_stored": r[6], "created_at": r[7]}
            for r in rows
        ]

    def get_atom_with_signals(self, memory_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, content, context_summary, memory_type, scope,
                       confidence, importance, support_weight, opposition_weight,
                       disagreement_score, last_recomputed_at, created_at,
                       lifecycle_status, superseded_by_atom_id, lifecycle_reason,
                       retrieval_priority, lifecycle_updated_at
                FROM memory_atoms WHERE id=?;
                """,
                (memory_id,),
            ).fetchone()
            if row is None:
                return None

            sig_count_row = conn.execute(
                "SELECT COUNT(*), MAX(created_at) FROM memory_signals WHERE memory_atom_id=?;",
                (memory_id,),
            ).fetchone()
            sig_count = int(sig_count_row[0]) if sig_count_row else 0
            sig_recent = sig_count_row[1] if sig_count_row else None

            top_sources = [
                r[0] for r in conn.execute(
                    "SELECT source_key FROM (SELECT source_key, MAX(created_at) AS last_seen "
                    "FROM memory_signals WHERE memory_atom_id=? GROUP BY source_key "
                    "ORDER BY last_seen DESC LIMIT 3) sub;",
                    (memory_id,),
                ).fetchall()
            ]

        ds = float(row[9]) if row[9] is not None else 0.0
        return {
            "id": row[0], "content": row[1], "context_summary": row[2],
            "memory_type": row[3], "scope": row[4],
            "confidence": float(row[5]), "importance": float(row[6]),
            "support_weight": float(row[7]) if row[7] is not None else 0.0,
            "opposition_weight": float(row[8]) if row[8] is not None else 0.0,
            "disagreement_score": ds, "disagreement_flag": ds >= 0.5,
            "last_recomputed_at": row[10],
            "created_at": row[11],
            "lifecycle_status": row[12] or "active",
            "superseded_by_atom_id": row[13],
            "lifecycle_reason": row[14],
            "retrieval_priority": float(row[15]) if row[15] is not None else 1.0,
            "lifecycle_updated_at": row[16],
            "signals_summary": {"count": sig_count, "top_sources": top_sources,
                                "most_recent_signal_at": sig_recent},
        }

    def get_atom_signals_db(self, memory_atom_id: str, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, memory_atom_id, parent_signal_id, source_key, source_type, source_id,
                       content, context_summary, memory_type, scope, subject, stance, relationship,
                       certainty, intensity, confidence, importance, reconciliation_reason, created_at
                FROM memory_signals WHERE memory_atom_id=? ORDER BY created_at DESC LIMIT ?;
                """,
                (memory_atom_id, limit),
            ).fetchall()
        return [
            {
                "id": r[0], "memory_atom_id": r[1], "parent_signal_id": r[2],
                "source_key": r[3], "source_type": r[4], "source_id": r[5],
                "content": r[6], "context_summary": r[7],
                "memory_type": r[8], "scope": r[9],
                "subject": r[10], "stance": r[11], "relationship": r[12],
                "certainty": float(r[13]) if r[13] is not None else None,
                "intensity": float(r[14]) if r[14] is not None else None,
                "confidence": float(r[15]) if r[15] is not None else None,
                "importance": float(r[16]) if r[16] is not None else None,
                "reconciliation_reason": r[17], "created_at": r[18],
            }
            for r in rows
        ]

    def search_memories_full(
        self,
        query: str,
        limit: int = 5,
        scope: str | None = None,
        memory_type: str | None = None,
        min_similarity: float = 0.0,
    ) -> list[dict[str, Any]]:
        """Semantic search with signals summary."""
        embedding = self.ollama.embed_text(query)
        clamped = max(0.0, min(float(min_similarity), 1.0))

        with self._connect() as conn:
            conditions = ["lifecycle_status != 'archived'"]
            params: list[Any] = []
            if scope:
                conditions.append("scope=?")
                params.append(scope)
            if memory_type:
                conditions.append("memory_type=?")
                params.append(memory_type)
            where = "WHERE " + " AND ".join(conditions)
            rows = conn.execute(
                f"""
                SELECT id, content, context_summary, memory_type, scope,
                       confidence, importance, embedding, created_at, support_weight,
                       opposition_weight, disagreement_score, last_recomputed_at,
                       lifecycle_status, superseded_by_atom_id, lifecycle_reason,
                       retrieval_priority, lifecycle_updated_at
                FROM memory_atoms {where};
                """,
                tuple(params),
            ).fetchall()

        scored: list[tuple[Any, float]] = []
        for row in rows:
            try:
                stored_emb = json.loads(row[7])
            except Exception:
                continue
            sim = _cosine(embedding, stored_emb)
            if sim >= clamped:
                scored.append((row, sim))
        scored.sort(key=lambda x: x[1], reverse=True)
        scored = scored[:limit]

        atom_ids = [row[0] for row, _ in scored]
        sigs: dict[str, Any] = {}
        if atom_ids:
            with self._connect() as conn:
                placeholders = ",".join("?" * len(atom_ids))
                sig_rows = conn.execute(
                    f"SELECT memory_atom_id, COUNT(*), MAX(created_at) FROM memory_signals "
                    f"WHERE memory_atom_id IN ({placeholders}) GROUP BY memory_atom_id;",
                    tuple(atom_ids),
                ).fetchall()
                for sg in sig_rows:
                    sigs[sg[0]] = {"count": int(sg[1]), "most_recent_signal_at": sg[2], "top_sources": []}
                if sigs:
                    src_rows = conn.execute(
                        f"SELECT memory_atom_id, source_key FROM ("
                        f"SELECT memory_atom_id, source_key, MAX(created_at) AS last_seen "
                        f"FROM memory_signals WHERE memory_atom_id IN ({placeholders}) "
                        f"GROUP BY memory_atom_id, source_key ORDER BY last_seen DESC) sub "
                        f"GROUP BY memory_atom_id HAVING COUNT(*) <= 3;",
                        tuple(atom_ids),
                    ).fetchall()
                    for sg in src_rows:
                        if sg[0] in sigs:
                            sigs[sg[0]]["top_sources"].append(sg[1])

        _empty = {"count": 0, "top_sources": [], "most_recent_signal_at": None}
        results = []
        for row, sim in scored:
            ds = float(row[11]) if row[11] is not None else 0.0
            results.append({
                "id": row[0], "content": row[1], "context_summary": row[2],
                "memory_type": row[3], "scope": row[4],
                "confidence": float(row[5]), "importance": float(row[6]),
                "similarity": sim,
                "created_at": row[8],
                "support_weight": float(row[9]) if row[9] is not None else 0.0,
                "opposition_weight": float(row[10]) if row[10] is not None else 0.0,
                "disagreement_score": ds,
                "last_recomputed_at": row[12],
                "disagreement_flag": ds >= 0.5,
                "lifecycle_status": row[13] or "active",
                "superseded_by_atom_id": row[14],
                "lifecycle_reason": row[15],
                "retrieval_priority": float(row[16]) if row[16] is not None else 1.0,
                "lifecycle_updated_at": row[17],
                "signals_summary": sigs.get(row[0], _empty),
            })
        return results

    def log_conversation_turn(
        self,
        user_message: str,
        assistant_response: str,
        retrieved_atom_ids: list[str],
        used_atom_ids: list[str],
        context_status: str,
        verdict: str,
        confidence: float,
        reasoning: str,
        source: str = "mcp_copilot",
        final_action: str = "answer",
    ) -> dict[str, Any]:
        task_summary = (user_message[:200] + "…") if len(user_message) > 200 else user_message
        now = _now()
        context_trace_id = _uid()
        response_trace_id = _uid()

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO runtime_context_traces
                    (id, task_summary, retrieved_atom_ids, used_atom_ids, ignored_atom_ids,
                     context_status, confidence, issues, required_actions, final_action, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (context_trace_id, task_summary,
                 json.dumps(retrieved_atom_ids), json.dumps(used_atom_ids), json.dumps([]),
                 context_status, confidence,
                 json.dumps([]), json.dumps([]), final_action, now),
            )
            conn.execute(
                """
                INSERT INTO runtime_response_traces
                    (id, user_message, draft_answer, final_answer, verdict,
                     overstatement_risk, issues, commit_candidates, reasoning,
                     context_trace_id, gap_status, gap_searches, gap_clarifying_question, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (response_trace_id,
                 (user_message or "")[:1000],
                 (assistant_response or "")[:4000],
                 (assistant_response or "")[:4000],
                 verdict, "none",
                 json.dumps([]), json.dumps([]),
                 (f"[{source}] " + (reasoning or ""))[:500],
                 context_trace_id, "resolved", 0, None, now),
            )
        return {"context_trace_id": context_trace_id,
                "response_trace_id": response_trace_id, "status": "logged"}

    def get_and_claim_proposal(
        self, proposal_id: str, approval_token: str
    ) -> dict[str, Any]:
        now = _now()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, status, content, context_summary, memory_type, scope, "
                "confidence, importance, relationship, reconciliation_reason, "
                "matched_memory_ids, approval_token, token_expires_at FROM memory_proposals WHERE id=?;",
                (proposal_id.strip(),),
            ).fetchone()

            if row is None:
                return {"error": f"proposal '{proposal_id}' not found"}

            p_id, p_status, p_content, p_context_summary, p_memory_type, p_scope, \
                p_confidence, p_importance, p_relationship, p_reconciliation_reason, \
                p_matched_ids_json, p_token, p_token_expires_at = row

            if p_status == "used":
                return {"error": "proposal already used"}
            if p_status == "rejected":
                return {"error": "proposal was rejected"}
            if p_status == "expired":
                return {"error": "proposal has expired"}
            if p_status != "approved":
                return {"error": f"proposal status is '{p_status}', expected 'approved'. Run `make review-proposals`."}
            if p_token is None or p_token != approval_token.strip():
                return {"error": "invalid approval token"}
            if p_token_expires_at is None or now > p_token_expires_at:
                conn.execute("UPDATE memory_proposals SET status='expired' WHERE id=?;", (p_id,))
                return {"error": "approval token has expired. Run `make review-proposals` again."}

            conn.execute(
                "UPDATE memory_proposals SET status='used', reviewed_at=? WHERE id=?;",
                (now, p_id),
            )

        return {
            "content": p_content, "context_summary": p_context_summary,
            "memory_type": p_memory_type, "scope": p_scope,
            "confidence": float(p_confidence), "importance": float(p_importance),
            "relationship": p_relationship, "reconciliation_reason": p_reconciliation_reason,
            "matched_memory_ids": p_matched_ids_json,
        }

    # ── store_context_trace / store_response_trace (used by commit_pipeline) ──

    def store_context_trace(self, trace: dict[str, Any]) -> str:
        trace_id = _uid()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO runtime_context_traces
                    (id, task_summary, retrieved_atom_ids, used_atom_ids, ignored_atom_ids,
                     context_status, confidence, issues, required_actions, final_action, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (trace_id,
                 (trace.get("task_summary") or "")[:500],
                 json.dumps(trace.get("retrieved_atom_ids") or []),
                 json.dumps(trace.get("used_atom_ids") or []),
                 json.dumps(trace.get("ignored_atom_ids") or []),
                 trace.get("context_status", "insufficient"),
                 trace.get("confidence"),
                 json.dumps(trace.get("issues") or []),
                 json.dumps(trace.get("required_actions") or []),
                 trace.get("final_action", "answer"),
                 _now()),
            )
        return trace_id

    def store_response_trace(self, trace: dict[str, Any]) -> str:
        trace_id = _uid()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO runtime_response_traces
                    (id, user_message, draft_answer, final_answer, verdict,
                     action_followed, overstatement_risk, issues, commit_candidates,
                     reasoning, context_trace_id, gap_status, gap_searches,
                     gap_clarifying_question, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (trace_id,
                 (trace.get("user_message") or "")[:1000],
                 (trace.get("draft_answer") or "")[:4000],
                 (trace.get("final_answer") or "")[:4000],
                 trace.get("verdict", "needs_caveat"),
                 trace.get("action_followed"),
                 trace.get("overstatement_risk", "low"),
                 json.dumps(trace.get("issues") or []),
                 json.dumps(trace.get("commit_candidates") or []),
                 (trace.get("reasoning") or "")[:500],
                 trace.get("context_trace_id") or None,
                 trace.get("gap_status") or None,
                 trace.get("gap_searches") or None,
                 trace.get("gap_clarifying_question") or None,
                 _now()),
            )
        return trace_id
