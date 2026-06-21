from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

import psycopg

from app.config import get_config
from app.llm_provider import LLMProvider, get_embedding_client, get_llm_client

# Memory types whose changes are tracked in belief_revision_log.
# These represent judgment / opinion rather than fixed fact.
REVISABLE_TYPES: frozenset[str] = frozenset({
    "opinion",
    "preference",
    "decision",
    "lesson",
    "belief",
})


class MemoryStore:
    def __init__(self, ollama_client: LLMProvider | None = None) -> None:
        self.config = get_config()
        self.ollama = ollama_client or get_embedding_client()

    @staticmethod
    def _vector_literal(embedding: list[float]) -> str:
        return "[" + ",".join(str(value) for value in embedding) + "]"

    @staticmethod
    def _normalize_text(value: str) -> str:
        return re.sub(r"\s+", " ", value.strip().lower())

    def init_db(self) -> None:
        try:
            from importlib.resources import files
            sql = files("app.sql").joinpath("init.sql").read_text(encoding="utf-8")
        except Exception:
            root_dir = Path(__file__).resolve().parent.parent
            sql = (root_dir / "db" / "init.sql").read_text(encoding="utf-8")

        with psycopg.connect(self.config.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
            conn.commit()

    def store_memory(
        self,
        content: str,
        context_summary: str | None = None,
        memory_type: str = "fact",
        scope: str | None = None,
        confidence: float = 0.8,
        importance: float = 0.5,
    ) -> str:
        summary_to_store = context_summary.strip() if isinstance(context_summary, str) and context_summary.strip() else content
        embedding = self.ollama.embed_text(content)
        embedding_literal = self._vector_literal(embedding)

        with psycopg.connect(self.config.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO memory_atoms (
                        content,
                        context_summary,
                        memory_type,
                        scope,
                        confidence,
                        importance,
                        embedding_model,
                        embedding
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s::vector)
                    RETURNING id;
                    """,
                    (
                        content,
                        summary_to_store,
                        memory_type,
                        scope,
                        confidence,
                        importance,
                        self.config.embedding_model,
                        embedding_literal,
                    ),
                )
                memory_id = cur.fetchone()[0]
            conn.commit()

        return str(memory_id)

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
        """Store a memory_atom and a linked memory_signal in a single transaction.

        The atom is inserted first; its id is set on the signal at creation time
        so no post-creation mutation is needed.

        ``atom_source_type`` and ``source_url`` are written to the atom's provenance
        columns (migration 013).  When omitted, ``atom_source_type`` falls back to
        ``source_type`` so existing callers are unaffected.
        """
        summary_to_store = (
            context_summary.strip()
            if isinstance(context_summary, str) and context_summary.strip()
            else content
        )
        embedding = self.ollama.embed_text(content)
        embedding_literal = self._vector_literal(embedding)
        metadata_json = json.dumps(signal_metadata) if signal_metadata else None

        with psycopg.connect(self.config.database_url) as conn:
            with conn.cursor() as cur:
                # Insert memory_atom first
                _atom_source_type = atom_source_type or source_type
                cur.execute(
                    """
                    INSERT INTO memory_atoms (
                        content, context_summary, memory_type, scope,
                        confidence, importance, embedding_model, embedding,
                        source_type, source_url
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s::vector, %s, %s)
                    RETURNING id;
                    """,
                    (
                        content,
                        summary_to_store,
                        memory_type,
                        scope,
                        confidence,
                        importance,
                        self.config.embedding_model,
                        embedding_literal,
                        _atom_source_type,
                        source_url,
                    ),
                )
                memory_id = cur.fetchone()[0]

                # Insert memory_signal with memory_atom_id already set
                cur.execute(
                    """
                    INSERT INTO memory_signals (
                        memory_atom_id, source_key, source_type,
                        content, context_summary, memory_type, scope,
                        relationship, confidence, importance,
                        raw_input, reconciliation_reason, metadata,
                        task_run_id
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
                    RETURNING id;
                    """,
                    (
                        memory_id,
                        source_key,
                        source_type,
                        content,
                        context_summary,
                        memory_type,
                        scope,
                        relationship,
                        confidence,
                        importance,
                        raw_input,
                        reconciliation_reason,
                        metadata_json,
                        task_run_id,
                    ),
                )
                signal_id = cur.fetchone()[0]
            conn.commit()

        # Auto-recompute aggregation weights now that the signal is committed.
        self.recompute_atom_weights(str(memory_id))

        return str(memory_id), str(signal_id)

    def recompute_atom_weights(self, atom_id: str) -> dict[str, Any] | None:
        """Recompute signal-aggregation weights for one atom and persist them.

        Fetches all linked memory_signals, calls signal_aggregator.compute_atom_weights,
        then writes support_weight, opposition_weight, disagreement_score, confidence,
        and last_recomputed_at back to memory_atoms.

        Returns the updated atom dict, or None if the atom does not exist.
        """
        from app.signal_aggregator import compute_atom_weights, compute_source_trust

        with psycopg.connect(self.config.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, memory_type FROM memory_atoms WHERE id = %s;",
                    (atom_id,),
                )
                atom_row = cur.fetchone()
                if atom_row is None:
                    return None
                atom_memory_type: str | None = atom_row[1]

                cur.execute(
                    """
                    SELECT relationship, confidence, source_key, created_at
                    FROM memory_signals
                    WHERE memory_atom_id = %s;
                    """,
                    (atom_id,),
                )
                rows = cur.fetchall()

                # Fetch global per-source stats for spam/trust computation.
                cur.execute(
                    """
                    SELECT source_key,
                           COUNT(*) AS total,
                           SUM(CASE WHEN relationship IN ('conflict', 'opinion_change')
                                    THEN 1 ELSE 0 END) AS conflicts
                    FROM memory_signals
                    GROUP BY source_key;
                    """,
                )
                source_rows = cur.fetchall()

            source_stats = [
                {"source_key": r[0], "total": r[1], "conflicts": r[2]}
                for r in source_rows
            ]
            source_trust = compute_source_trust(source_stats)

            signals = [
                {
                    "relationship": row[0],
                    "confidence": row[1],
                    "source_key": row[2],
                    "created_at": row[3],
                }
                for row in rows
            ]

            weights = compute_atom_weights(signals, memory_type=atom_memory_type, source_trust=source_trust)

            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE memory_atoms
                    SET support_weight     = %s,
                        opposition_weight  = %s,
                        disagreement_score = %s,
                        confidence         = %s,
                        retrieval_priority = %s,
                        last_recomputed_at = now()
                    WHERE id = %s
                    RETURNING
                        id, content, context_summary, memory_type, scope,
                        confidence, importance, support_weight, opposition_weight,
                        disagreement_score, last_recomputed_at, created_at;
                    """,
                    (
                        weights["support_weight"],
                        weights["opposition_weight"],
                        weights["disagreement_score"],
                        weights["confidence"],
                        weights.get("retrieval_priority", 1.0),
                        atom_id,
                    ),
                )
                row = cur.fetchone()
            conn.commit()

        if row is None:
            return None

        return {
            "id": str(row[0]),
            "content": row[1],
            "context_summary": row[2],
            "memory_type": row[3],
            "scope": row[4],
            "confidence": float(row[5]),
            "importance": float(row[6]),
            "support_weight": float(row[7]),
            "opposition_weight": float(row[8]),
            "disagreement_score": float(row[9]),
            "last_recomputed_at": row[10].isoformat() if row[10] else None,
            "created_at": row[11].isoformat() if row[11] else None,
        }

    def find_near_duplicates(
        self,
        content: str,
        threshold: float = 0.93,
        limit: int = 3,
    ) -> list[dict[str, Any]]:
        embedding = self.ollama.embed_text(content)
        embedding_literal = self._vector_literal(embedding)

        with psycopg.connect(self.config.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        id,
                        content,
                        context_summary,
                        memory_type,
                        scope,
                        confidence,
                        importance,
                        1 - (embedding <=> %s::vector) AS similarity
                    FROM memory_atoms
                    ORDER BY embedding <=> %s::vector
                    LIMIT %s;
                    """,
                    (embedding_literal, embedding_literal, limit),
                )
                rows = cur.fetchall()

        duplicates: list[dict[str, Any]] = []
        for row in rows:
            similarity = float(row[7])
            if similarity >= threshold:
                duplicates.append(
                    {
                        "id": str(row[0]),
                        "content": row[1],
                        "context_summary": row[2],
                        "memory_type": row[3],
                        "scope": row[4],
                        "confidence": float(row[5]),
                        "importance": float(row[6]),
                        "similarity": similarity,
                    }
                )

        return duplicates

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
        duplicates = self.find_near_duplicates(
            content=content,
            threshold=dupe_threshold,
            limit=dupe_limit,
        )
        if duplicates:
            incoming_summary = (
                context_summary.strip()
                if isinstance(context_summary, str) and context_summary.strip()
                else None
            )

            if incoming_summary:
                top_duplicate = duplicates[0]
                existing_summary = top_duplicate.get("context_summary")
                has_existing_summary = isinstance(existing_summary, str) and existing_summary.strip()
                if not has_existing_summary:
                    with psycopg.connect(self.config.database_url) as conn:
                        with conn.cursor() as cur:
                            cur.execute(
                                """
                                UPDATE memory_atoms
                                SET context_summary = %s
                                WHERE id = %s::uuid
                                  AND (context_summary IS NULL OR btrim(context_summary) = '');
                                """,
                                (incoming_summary, top_duplicate["id"]),
                            )
                        conn.commit()

                    top_duplicate["context_summary"] = incoming_summary
                    top_duplicate["context_summary_updated"] = True

            return None, duplicates

        memory_id = self.store_memory(
            content=content,
            context_summary=context_summary,
            memory_type=memory_type,
            scope=scope,
            confidence=confidence,
            importance=importance,
        )
        return memory_id, []

    def list_memories(
        self,
        scope: str | None = None,
        memory_type: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        where_clauses: list[str] = []
        params: list[Any] = []

        if scope:
            where_clauses.append("scope = %s")
            params.append(scope)
        if memory_type:
            where_clauses.append("memory_type = %s")
            params.append(memory_type)

        where_sql = ""
        if where_clauses:
            where_sql = "WHERE " + " AND ".join(where_clauses)

        params.append(limit)

        with psycopg.connect(self.config.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT
                        id,
                        memory_type,
                        scope,
                        content,
                        context_summary,
                        created_at
                    FROM memory_atoms
                    {where_sql}
                    ORDER BY created_at DESC
                    LIMIT %s;
                    """,
                    tuple(params),
                )
                rows = cur.fetchall()

        results: list[dict[str, Any]] = []
        for row in rows:
            results.append(
                {
                    "id": str(row[0]),
                    "memory_type": row[1],
                    "scope": row[2],
                    "content": row[3],
                    "context_summary": row[4],
                    "created_at": row[5].isoformat() if row[5] is not None else None,
                }
            )

        return results

    def find_duplicate_candidate(
        self,
        content: str,
        memory_type: str,
        scope: str | None,
        dupe_threshold: float = 0.93,
        dupe_limit: int = 3,
    ) -> dict[str, Any] | None:
        exact_match = self.find_exact_content_match(content)
        if exact_match:
            exact_match["match_type"] = "exact"
            return exact_match

        near_matches = self.find_near_duplicates(
            content=content,
            threshold=dupe_threshold,
            limit=dupe_limit,
        )
        for match in near_matches:
            same_type = match.get("memory_type") == memory_type
            same_scope = (match.get("scope") or None) == (scope or None)
            if same_type and same_scope:
                match["match_type"] = "near"
                return match

        return None

    def find_exact_content_match(self, content: str) -> dict[str, Any] | None:
        normalized_content = self._normalize_text(content)

        with psycopg.connect(self.config.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        id,
                        content,
                        context_summary,
                        memory_type,
                        scope,
                        confidence,
                        importance,
                        created_at
                    FROM memory_atoms
                    WHERE lower(regexp_replace(btrim(content), '\\s+', ' ', 'g')) = %s
                    ORDER BY created_at DESC
                    LIMIT 1;
                    """,
                    (normalized_content,),
                )
                row = cur.fetchone()

        if not row:
            return None

        return {
            "id": str(row[0]),
            "content": row[1],
            "context_summary": row[2],
            "memory_type": row[3],
            "scope": row[4],
            "confidence": float(row[5]),
            "importance": float(row[6]),
            "created_at": row[7].isoformat() if row[7] is not None else None,
            "similarity": 1.0,
        }

    def get_memory(self, memory_id: str) -> dict[str, Any] | None:
        with psycopg.connect(self.config.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        id,
                        memory_type,
                        scope,
                        content,
                        context_summary,
                        confidence,
                        importance,
                        embedding_model,
                        created_at
                    FROM memory_atoms
                    WHERE id = %s::uuid;
                    """,
                    (memory_id,),
                )
                row = cur.fetchone()

        if not row:
            return None

        return {
            "id": str(row[0]),
            "memory_type": row[1],
            "scope": row[2],
            "content": row[3],
            "context_summary": row[4],
            "confidence": float(row[5]),
            "importance": float(row[6]),
            "embedding_model": row[7],
            "created_at": row[8].isoformat() if row[8] is not None else None,
        }

    def delete_memory(self, memory_id: str) -> bool:
        with psycopg.connect(self.config.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM memory_atoms WHERE id = %s::uuid;",
                    (memory_id,),
                )
                deleted = cur.rowcount > 0
            conn.commit()
        return deleted

    def update_memory(
        self,
        memory_id: str,
        updates: dict[str, Any],
    ) -> tuple[bool, bool]:
        if not updates:
            return False, False

        set_clauses: list[str] = []
        params: list[Any] = []
        embedding_regenerated = False

        if "content" in updates:
            new_content = str(updates["content"]).strip()
            if not new_content:
                raise ValueError("content cannot be empty")
            set_clauses.append("content = %s")
            params.append(new_content)

            embedding = self.ollama.embed_text(new_content)
            embedding_literal = self._vector_literal(embedding)
            set_clauses.append("embedding = %s::vector")
            params.append(embedding_literal)
            set_clauses.append("embedding_model = %s")
            params.append(self.config.embedding_model)
            embedding_regenerated = True

        if "context_summary" in updates:
            summary = updates["context_summary"]
            if isinstance(summary, str):
                summary = summary.strip()
            if summary == "":
                summary = None
            set_clauses.append("context_summary = %s")
            params.append(summary)

        if "memory_type" in updates:
            set_clauses.append("memory_type = %s")
            params.append(str(updates["memory_type"]))

        if "scope" in updates:
            scope = updates["scope"]
            if isinstance(scope, str):
                scope = scope.strip()
            if scope == "":
                scope = None
            set_clauses.append("scope = %s")
            params.append(scope)

        if "confidence" in updates:
            set_clauses.append("confidence = %s")
            params.append(float(updates["confidence"]))

        if "importance" in updates:
            set_clauses.append("importance = %s")
            params.append(float(updates["importance"]))

        if not set_clauses:
            return False, False

        params.append(memory_id)
        query = (
            "UPDATE memory_atoms "
            f"SET {', '.join(set_clauses)} "
            "WHERE id = %s::uuid"
        )

        with psycopg.connect(self.config.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(query, tuple(params))
                updated = cur.rowcount > 0
            conn.commit()

        return updated, embedding_regenerated

    # ── Commit pipeline helpers ───────────────────────────────────────────────

    def add_signal_to_atom(
        self,
        atom_id: str,
        content: str,
        relationship: str = "reinforcement",
        context_summary: str | None = None,
        memory_type: str = "fact",
        scope: str | None = None,
        confidence: float = 0.8,
        importance: float = 0.5,
        source_key: str = "local_user",
        source_type: str = "local",
        reconciliation_reason: str | None = None,
    ) -> str:
        """Add a memory_signal to an existing atom (no new atom created).

        Triggers weight recomputation on the atom after the signal is inserted.
        Returns the new signal UUID.
        """
        summary = (context_summary or "").strip() or content
        with psycopg.connect(self.config.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO memory_signals (
                        memory_atom_id, source_key, source_type,
                        content, context_summary, memory_type, scope,
                        relationship, confidence, importance,
                        reconciliation_reason
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id;
                    """,
                    (
                        atom_id, source_key, source_type,
                        content, summary, memory_type, scope,
                        relationship, confidence, importance,
                        reconciliation_reason,
                    ),
                )
                signal_id = str(cur.fetchone()[0])
            conn.commit()
        self.recompute_atom_weights(atom_id)
        return signal_id

    def store_proposal(
        self,
        content: str,
        memory_type: str,
        relationship: str,
        context_summary: str | None = None,
        scope: str | None = None,
        confidence: float = 0.8,
        importance: float = 0.5,
        reconciliation_reason: str | None = None,
        matched_memory_ids: list[str] | None = None,
    ) -> str:
        """Queue a candidate as a pending proposal for human review.

        Inserts into memory_proposals with status='pending_review'.
        Does NOT write to memory_atoms or memory_signals.
        Returns the proposal UUID.
        """
        summary = (context_summary or "").strip() or content
        matched_ids_json = json.dumps(matched_memory_ids) if matched_memory_ids else None

        with psycopg.connect(self.config.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO memory_proposals (
                        content, context_summary, memory_type, scope,
                        confidence, importance,
                        relationship, reconciliation_reason, matched_memory_ids
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id;
                    """,
                    (
                        content.strip(),
                        summary,
                        memory_type.strip(),
                        scope,
                        max(0.0, min(1.0, float(confidence))),
                        max(0.0, min(1.0, float(importance))),
                        relationship,
                        reconciliation_reason,
                        matched_ids_json,
                    ),
                )
                row = cur.fetchone()
            conn.commit()
        return str(row[0])

    def store_commit_trace(self, trace: dict[str, Any]) -> str:
        """Insert a commit pipeline trace record. Returns the trace UUID."""
        with psycopg.connect(self.config.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO memory_commit_traces (
                        candidate_text, final_memory_text, decision,
                        write_action, memory_type, scope, confidence,
                        lifecycle_action,
                        duplicate_atom_ids, reinforces_atom_ids,
                        refines_atom_ids, supersedes_atom_ids,
                        conflicts_with_atom_ids,
                        committed_atom_id, proposal_id,
                        critic_notes, rejection_reason
                    )
                    VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s,
                        %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb,
                        %s, %s,
                        %s::jsonb, %s
                    )
                    RETURNING id;
                    """,
                    (
                        trace.get("candidate_text", ""),
                        trace.get("final_memory_text"),
                        trace.get("decision", "reject"),
                        trace.get("write_action"),
                        trace.get("memory_type"),
                        trace.get("scope"),
                        trace.get("confidence"),
                        trace.get("lifecycle_action"),
                        json.dumps(trace.get("duplicate_atom_ids") or []),
                        json.dumps(trace.get("reinforces_atom_ids") or []),
                        json.dumps(trace.get("refines_atom_ids") or []),
                        json.dumps(trace.get("supersedes_atom_ids") or []),
                        json.dumps(trace.get("conflicts_with_atom_ids") or []),
                        trace.get("committed_atom_id") or None,
                        trace.get("proposal_id") or None,
                        json.dumps(trace.get("critic_notes") or []),
                        trace.get("rejection_reason"),
                    ),
                )
                trace_id = str(cur.fetchone()[0])
            conn.commit()
        return trace_id

    def list_commit_traces(
        self,
        limit: int = 50,
        decision_filter: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return recent commit trace records, newest first."""
        params: list[Any] = []
        where = ""
        if decision_filter:
            where = "WHERE decision = %s"
            params.append(decision_filter)
        params.append(limit)

        with psycopg.connect(self.config.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT
                        id, candidate_text, final_memory_text, decision,
                        write_action, memory_type, scope, confidence,
                        lifecycle_action,
                        duplicate_atom_ids, reinforces_atom_ids,
                        refines_atom_ids, supersedes_atom_ids,
                        conflicts_with_atom_ids,
                        committed_atom_id, proposal_id,
                        critic_notes, rejection_reason, created_at
                    FROM memory_commit_traces
                    {where}
                    ORDER BY created_at DESC
                    LIMIT %s;
                    """,
                    tuple(params),
                )
                rows = cur.fetchall()

        results: list[dict[str, Any]] = []
        for row in rows:
            results.append({
                "id": str(row[0]),
                "candidate_text": row[1],
                "final_memory_text": row[2],
                "decision": row[3],
                "write_action": row[4],
                "memory_type": row[5],
                "scope": row[6],
                "confidence": float(row[7]) if row[7] is not None else None,
                "lifecycle_action": row[8],
                "duplicate_atom_ids": row[9] or [],
                "reinforces_atom_ids": row[10] or [],
                "refines_atom_ids": row[11] or [],
                "supersedes_atom_ids": row[12] or [],
                "conflicts_with_atom_ids": row[13] or [],
                "committed_atom_id": str(row[14]) if row[14] else None,
                "proposal_id": str(row[15]) if row[15] else None,
                "critic_notes": row[16] or [],
                "rejection_reason": row[17],
                "created_at": row[18].isoformat() if row[18] else None,
            })
        return results

    def retrieve_memories(
        self,
        query: str,
        limit: int = 5,
        min_similarity: float | None = None,
        scope_filter: str | None = None,
        scope_filters: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        # Normalise singular/plural scope args into one list.
        all_scopes: list[str] | None = None
        if scope_filters:
            all_scopes = list(scope_filters)
        if scope_filter and (not all_scopes or scope_filter not in all_scopes):
            all_scopes = (all_scopes or []) + [scope_filter]

        embedding = self.ollama.embed_text(query)
        embedding_literal = self._vector_literal(embedding)
        threshold = min_similarity if min_similarity is not None else self.config.memory_retrieval_threshold

        # Scope clause uses ANY() for multi-scope; values go through %s parameterisation.
        scope_clause = "AND (scope = ANY(%s::text[]) OR scope IS NULL OR scope = 'global')" if all_scopes else ""
        scope_params: tuple = (all_scopes,) if all_scopes else ()

        with psycopg.connect(self.config.database_url) as conn:
            with conn.cursor() as cur:
                # CTE computes similarity once; outer query applies composite
                # trust-ranked scoring and recency decay for volatile types.
                cur.execute(
                    f"""
                    WITH scored AS (
                        SELECT
                            id,
                            content,
                            context_summary,
                            memory_type,
                            scope,
                            confidence,
                            importance,
                            1 - (embedding <=> %s::vector) AS similarity,
                            created_at,
                            lifecycle_status,
                            support_weight,
                            opposition_weight,
                            disagreement_score
                        FROM memory_atoms
                        WHERE (lifecycle_status IS NULL
                               OR lifecycle_status NOT IN ('superseded', 'deprecated', 'archived'))
                          {scope_clause}
                    )
                    SELECT *,
                        (
                            similarity * 0.60
                            + confidence * 0.25
                            - COALESCE(disagreement_score, 0.0) * 0.15
                        ) * CASE
                            WHEN memory_type IN ('opinion','preference','lesson','belief')
                            THEN GREATEST(0.3, EXP(
                                -LN(2) * EXTRACT(EPOCH FROM (NOW() - created_at)) / (90.0 * 86400)
                            ))
                            ELSE 1.0
                        END AS composite_score
                    FROM scored
                    WHERE similarity >= %s
                    ORDER BY composite_score DESC
                    LIMIT %s;
                    """,
                    (embedding_literal, *scope_params, threshold, limit),
                )
                rows = cur.fetchall()

        results: list[dict[str, Any]] = []
        for row in rows:
            ds = float(row[12]) if row[12] is not None else 0.0
            results.append(
                {
                    "id": str(row[0]),
                    "content": row[1],
                    "context_summary": row[2],
                    "memory_type": row[3],
                    "scope": row[4],
                    "confidence": float(row[5]),
                    "importance": float(row[6]),
                    "similarity": float(row[7]),
                    "composite_score": round(float(row[13]), 4),
                    "created_at": row[8].isoformat() if row[8] is not None else None,
                    "lifecycle_status": row[9] or "active",
                    "support_weight": float(row[10]) if row[10] is not None else 0.0,
                    "opposition_weight": float(row[11]) if row[11] is not None else 0.0,
                    "disagreement_score": ds,
                    "disagreement_flag": ds >= 0.5,
                }
            )

        return results

    def store_context_trace(self, trace: dict[str, Any]) -> str:
        """Insert a runtime context trace record. Returns the trace UUID."""
        with psycopg.connect(self.config.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO runtime_context_traces (
                        task_summary, retrieved_atom_ids, used_atom_ids,
                        ignored_atom_ids, context_status, confidence,
                        issues, required_actions, final_action
                    )
                    VALUES (
                        %s, %s::jsonb, %s::jsonb,
                        %s::jsonb, %s, %s,
                        %s::jsonb, %s::jsonb, %s
                    )
                    RETURNING id;
                    """,
                    (
                        trace.get("task_summary", "")[:500],
                        json.dumps(trace.get("retrieved_atom_ids") or []),
                        json.dumps(trace.get("used_atom_ids") or []),
                        json.dumps(trace.get("ignored_atom_ids") or []),
                        trace.get("context_status", "insufficient"),
                        trace.get("confidence"),
                        json.dumps(trace.get("issues") or []),
                        json.dumps(trace.get("required_actions") or []),
                        trace.get("final_action", "answer"),
                    ),
                )
                trace_id = str(cur.fetchone()[0])
            conn.commit()
        return trace_id

    def list_context_traces(
        self,
        limit: int = 50,
        status_filter: str | None = None,
        action_filter: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return recent context trace records, newest first."""
        conditions: list[str] = []
        params: list[Any] = []
        if status_filter:
            conditions.append("context_status = %s")
            params.append(status_filter)
        if action_filter:
            conditions.append("final_action = %s")
            params.append(action_filter)
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        params.append(limit)

        with psycopg.connect(self.config.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT
                        id, task_summary, retrieved_atom_ids, used_atom_ids,
                        ignored_atom_ids, context_status, confidence,
                        issues, required_actions, final_action, created_at
                    FROM runtime_context_traces
                    {where}
                    ORDER BY created_at DESC
                    LIMIT %s;
                    """,
                    tuple(params),
                )
                rows = cur.fetchall()

        results: list[dict[str, Any]] = []
        for row in rows:
            results.append({
                "id": str(row[0]),
                "task_summary": row[1],
                "retrieved_atom_ids": row[2] or [],
                "used_atom_ids": row[3] or [],
                "ignored_atom_ids": row[4] or [],
                "context_status": row[5],
                "confidence": float(row[6]) if row[6] is not None else None,
                "issues": row[7] or [],
                "required_actions": row[8] or [],
                "final_action": row[9],
                "created_at": row[10].isoformat() if row[10] else None,
            })
        return results

    def store_response_trace(self, trace: dict[str, Any]) -> str:
        """Insert a runtime response evaluation trace. Returns the trace UUID."""
        with psycopg.connect(self.config.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO runtime_response_traces (
                        user_message, draft_answer, final_answer,
                        verdict, action_followed, overstatement_risk,
                        issues, commit_candidates, reasoning, context_trace_id,
                        gap_status, gap_searches, gap_clarifying_question
                    )
                    VALUES (
                        %s, %s, %s,
                        %s, %s, %s,
                        %s::jsonb, %s::jsonb, %s,
                        %s::uuid,
                        %s, %s, %s
                    )
                    RETURNING id;
                    """,
                    (
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
                        (trace.get("gap_clarifying_question") or None),
                    ),
                )
                trace_id = str(cur.fetchone()[0])
            conn.commit()
        return trace_id

    def list_response_traces(
        self,
        limit: int = 50,
        verdict_filter: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return recent response trace records, newest first."""
        conditions: list[str] = []
        params: list[Any] = []
        if verdict_filter:
            conditions.append("verdict = %s")
            params.append(verdict_filter)
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        params.append(limit)

        with psycopg.connect(self.config.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT
                        id, user_message, draft_answer, final_answer,
                        verdict, action_followed, overstatement_risk,
                        issues, commit_candidates, reasoning,
                        context_trace_id, created_at,
                        gap_status, gap_searches, gap_clarifying_question
                    FROM runtime_response_traces
                    {where}
                    ORDER BY created_at DESC
                    LIMIT %s;
                    """,
                    tuple(params),
                )
                rows = cur.fetchall()

        results: list[dict[str, Any]] = []
        for row in rows:
            results.append({
                "id": str(row[0]),
                "user_message": row[1],
                "draft_answer": row[2],
                "final_answer": row[3],
                "verdict": row[4],
                "action_followed": row[5],
                "overstatement_risk": row[6],
                "issues": row[7] or [],
                "commit_candidates": row[8] or [],
                "reasoning": row[9],
                "context_trace_id": str(row[10]) if row[10] else None,
                "created_at": row[11].isoformat() if row[11] else None,
                "gap_status": row[12],
                "gap_searches": row[13],
                "gap_clarifying_question": row[14],
            })
        return results

    # ── Belief revision log ───────────────────────────────────────────────────

    def log_belief_revision(
        self,
        atom_id: str,
        new_content: str,
        new_confidence: float,
        memory_type: str,
        event_type: str,
        scope: str | None = None,
        prior_atom_id: str | None = None,
        prior_content: str | None = None,
        prior_confidence: float | None = None,
        revision_reason: str | None = None,
        source_key: str = "local_user",
    ) -> str:
        """Write one entry to belief_revision_log. Returns the log entry UUID.

        Args:
            atom_id: UUID of the atom that exists AFTER this event.
                     For reinforce this is the same atom (in-place).
            new_content: Full text of the belief after the event.
            new_confidence: Confidence after the event.
            memory_type: Memory type of the belief (must be in REVISABLE_TYPES).
            event_type: One of 'commit', 'refine', 'supersede', 'reinforce'.
            scope: Scope of the belief.
            prior_atom_id: UUID of the atom being replaced (None for commit).
            prior_content: Full text before the event (None for initial commit).
            prior_confidence: Confidence before the event (None for initial commit).
            revision_reason: Human-readable reason from the commit pipeline.
            source_key: Who/what produced the change.
        """
        with psycopg.connect(self.config.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO belief_revision_log (
                        atom_id, prior_atom_id,
                        prior_content, new_content,
                        prior_confidence, new_confidence,
                        memory_type, scope,
                        event_type, revision_reason, source_key
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id;
                    """,
                    (
                        atom_id,
                        prior_atom_id,
                        prior_content,
                        new_content,
                        prior_confidence,
                        new_confidence,
                        memory_type,
                        scope,
                        event_type,
                        revision_reason,
                        source_key,
                    ),
                )
                log_id = str(cur.fetchone()[0])
            conn.commit()
        return log_id

    def get_belief_detail(self, atom_id: str) -> dict[str, Any] | None:
        """Return the full belief picture for one atom.

        Combines the atom itself, its supporting and opposing signals,
        and the complete revision history from birth through every change.

        Returns:
            {
                "atom": {...},
                "signals": {
                    "supporting": [...],
                    "opposing":   [...],
                },
                "revision_history": [...]  # oldest-first
            }
            or None if the atom does not exist.
        """
        with psycopg.connect(self.config.database_url) as conn:
            with conn.cursor() as cur:
                # ── Atom ─────────────────────────────────────────────────────
                cur.execute(
                    """
                    SELECT
                        id, content, context_summary, memory_type, scope,
                        confidence, importance,
                        support_weight, opposition_weight, disagreement_score,
                        lifecycle_status, lifecycle_reason,
                        superseded_by_atom_id, created_at, last_recomputed_at
                    FROM memory_atoms
                    WHERE id = %s;
                    """,
                    (atom_id,),
                )
                row = cur.fetchone()
                if row is None:
                    return None

                atom: dict[str, Any] = {
                    "id": str(row[0]),
                    "content": row[1],
                    "context_summary": row[2],
                    "memory_type": row[3],
                    "scope": row[4],
                    "confidence": float(row[5]),
                    "importance": float(row[6]),
                    "support_weight": float(row[7]) if row[7] is not None else 0.0,
                    "opposition_weight": float(row[8]) if row[8] is not None else 0.0,
                    "disagreement_score": float(row[9]) if row[9] is not None else 0.0,
                    "lifecycle_status": row[10] or "active",
                    "lifecycle_reason": row[11],
                    "superseded_by_atom_id": str(row[12]) if row[12] else None,
                    "created_at": row[13].isoformat() if row[13] else None,
                    "last_recomputed_at": row[14].isoformat() if row[14] else None,
                }

                # ── Signals ───────────────────────────────────────────────────
                cur.execute(
                    """
                    SELECT id, content, relationship, confidence,
                           source_key, reconciliation_reason, created_at
                    FROM memory_signals
                    WHERE memory_atom_id = %s
                    ORDER BY created_at ASC;
                    """,
                    (atom_id,),
                )
                signal_rows = cur.fetchall()

                supporting: list[dict[str, Any]] = []
                opposing: list[dict[str, Any]] = []
                for sr in signal_rows:
                    sig: dict[str, Any] = {
                        "id": str(sr[0]),
                        "content": sr[1],
                        "relationship": sr[2],
                        "confidence": float(sr[3]) if sr[3] is not None else None,
                        "source_key": sr[4],
                        "reason": sr[5],
                        "created_at": sr[6].isoformat() if sr[6] else None,
                    }
                    rel = (sr[2] or "").lower()
                    if rel in ("conflict", "opposition", "opinion_change"):
                        opposing.append(sig)
                    else:
                        supporting.append(sig)

                # ── Revision history ──────────────────────────────────────────
                cur.execute(
                    """
                    SELECT
                        id, prior_atom_id,
                        prior_content, new_content,
                        prior_confidence, new_confidence,
                        event_type, revision_reason, source_key, revised_at
                    FROM belief_revision_log
                    WHERE atom_id = %s
                    ORDER BY revised_at ASC;
                    """,
                    (atom_id,),
                )
                rev_rows = cur.fetchall()
                revision_history = [
                    {
                        "id": str(r[0]),
                        "prior_atom_id": str(r[1]) if r[1] else None,
                        "prior_content": r[2],
                        "new_content": r[3],
                        "prior_confidence": float(r[4]) if r[4] is not None else None,
                        "new_confidence": float(r[5]),
                        "event_type": r[6],
                        "revision_reason": r[7],
                        "source_key": r[8],
                        "revised_at": r[9].isoformat() if r[9] else None,
                    }
                    for r in rev_rows
                ]

        return {
            "atom": atom,
            "signals": {
                "supporting": supporting,
                "opposing": opposing,
            },
            "revision_history": revision_history,
        }

    # ── Shared interface methods (also implemented by SQLiteStore) ────────────

    def health_stats(self) -> dict[str, Any]:
        """Return atom count and available scopes."""
        with psycopg.connect(self.config.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM memory_atoms;")
                count = int(cur.fetchone()[0])
                cur.execute("SELECT DISTINCT scope FROM memory_atoms ORDER BY scope;")
                scopes = [r[0] for r in cur.fetchall()]
        return {"atom_count": count, "available_scopes": scopes, "backend": "postgres"}

    def health_report(self) -> dict[str, Any]:
        """Return a detailed health report for the dashboard /health page."""
        with psycopg.connect(self.config.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COALESCE(lifecycle_status,'active'), COUNT(*) FROM memory_atoms GROUP BY lifecycle_status;"
                )
                lifecycle_rows = cur.fetchall()

                cur.execute(
                    """
                    SELECT COUNT(*) AS total_active,
                           SUM(CASE WHEN disagreement_score >= 0.4 THEN 1 ELSE 0 END) AS contested,
                           SUM(CASE WHEN support_weight = 0 THEN 1 ELSE 0 END) AS orphans,
                           AVG(confidence) AS avg_confidence,
                           AVG(disagreement_score) AS avg_disagreement
                    FROM memory_atoms
                    WHERE lifecycle_status = 'active' OR lifecycle_status IS NULL;
                    """
                )
                active_stats = cur.fetchone()

                cur.execute(
                    """
                    SELECT COALESCE(scope,'(none)'), COUNT(*)
                    FROM memory_atoms
                    WHERE lifecycle_status = 'active' OR lifecycle_status IS NULL
                    GROUP BY scope ORDER BY COUNT(*) DESC LIMIT 12;
                    """
                )
                scope_rows = cur.fetchall()

                cur.execute(
                    """
                    SELECT memory_type, COUNT(*)
                    FROM memory_atoms
                    WHERE lifecycle_status = 'active' OR lifecycle_status IS NULL
                    GROUP BY memory_type ORDER BY COUNT(*) DESC;
                    """
                )
                type_rows = cur.fetchall()

                cur.execute(
                    """
                    SELECT id::text, content, disagreement_score, confidence, COALESCE(scope,'(none)')
                    FROM memory_atoms
                    WHERE (lifecycle_status = 'active' OR lifecycle_status IS NULL)
                      AND disagreement_score >= 0.4
                    ORDER BY disagreement_score DESC LIMIT 5;
                    """
                )
                contested_rows = cur.fetchall()

                cur.execute(
                    "SELECT COUNT(DISTINCT memory_atom_id), COUNT(*) FROM memory_signals;"
                )
                signal_stats = cur.fetchone()

                cur.execute(
                    """
                    SELECT date_trunc('day', created_at)::date::text AS day, COUNT(*)
                    FROM memory_signals
                    WHERE created_at >= NOW() - INTERVAL '14 days'
                    GROUP BY day ORDER BY day DESC;
                    """
                )
                recent_signals = cur.fetchall()

        lifecycle = {row[0]: int(row[1]) for row in lifecycle_rows}
        active = int(active_stats[0] or 0)

        def pct(n: int) -> float:
            return round(n / active * 100, 1) if active else 0.0

        return {
            "backend": "postgres",
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
        }

    def list_recent(
        self, limit: int = 10, scope: str | None = None
    ) -> list[dict[str, Any]]:
        """Return most recent non-archived atoms."""
        params: list[Any] = []
        scope_clause = ""
        if scope is not None:
            scope_clause = "AND scope = %s"
            params.append(scope)
        params.append(limit)

        with psycopg.connect(self.config.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT id, content, context_summary, memory_type, scope,
                           confidence, importance, created_at,
                           support_weight, opposition_weight, disagreement_score,
                           last_recomputed_at, lifecycle_status,
                           superseded_by_atom_id, lifecycle_reason,
                           retrieval_priority, lifecycle_updated_at
                    FROM memory_atoms
                    WHERE lifecycle_status != 'archived' {scope_clause}
                    ORDER BY created_at DESC LIMIT %s;
                    """,
                    tuple(params),
                )
                rows = cur.fetchall()

        return [_pg_atom_row_to_dict(r) for r in rows]

    def project_context_atoms(
        self,
        scope: str,
        limit: int = 10,
        min_importance: float = 0.6,
        min_confidence: float = 0.7,
    ) -> list[dict[str, Any]]:
        """Return high-importance, high-confidence active atoms for a scope."""
        with psycopg.connect(self.config.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, content, context_summary, memory_type, scope,
                           confidence, importance, created_at,
                           support_weight, opposition_weight, disagreement_score,
                           last_recomputed_at, lifecycle_status,
                           superseded_by_atom_id, lifecycle_reason,
                           retrieval_priority, lifecycle_updated_at
                    FROM memory_atoms
                    WHERE scope = %s AND importance >= %s AND confidence >= %s
                      AND lifecycle_status = 'active'
                    ORDER BY importance DESC, confidence DESC, created_at DESC
                    LIMIT %s;
                    """,
                    (scope, min_importance, min_confidence, limit),
                )
                rows = cur.fetchall()
        return [_pg_atom_row_to_dict(r) for r in rows]

    def get_active_atoms_by_scope(
        self, scope: str, limit: int = 20
    ) -> list[dict[str, Any]]:
        """Return all active atoms for a scope (used for model_lessons)."""
        with psycopg.connect(self.config.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, content, context_summary, memory_type, scope,
                           confidence, importance, created_at,
                           support_weight, opposition_weight, disagreement_score,
                           last_recomputed_at, lifecycle_status,
                           superseded_by_atom_id, lifecycle_reason,
                           retrieval_priority, lifecycle_updated_at
                    FROM memory_atoms
                    WHERE scope = %s AND lifecycle_status = 'active'
                    ORDER BY importance DESC, confidence DESC, created_at DESC
                    LIMIT %s;
                    """,
                    (scope, limit),
                )
                rows = cur.fetchall()
        return [_pg_atom_row_to_dict(r) for r in rows]

    def get_stale_atoms(
        self,
        days_threshold: int = 90,
        min_disagreement: float = 0.4,
        scope: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Return active atoms that are stale: old+under-supported, contested, or volatile+aged."""
        scope_clause = "AND scope = %s" if scope else ""
        scope_param: tuple = (scope,) if scope else ()

        with psycopg.connect(self.config.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT id, content, memory_type, scope, confidence,
                           support_weight, disagreement_score, created_at,
                           EXTRACT(EPOCH FROM (NOW() - created_at)) / 86400 AS age_days
                    FROM memory_atoms
                    WHERE lifecycle_status = 'active'
                      {scope_clause}
                      AND (
                        (NOW() - created_at > make_interval(days => %s) AND support_weight < 0.5)
                        OR disagreement_score >= %s
                        OR (memory_type IN ('opinion','preference','lesson','belief')
                            AND NOW() - created_at > INTERVAL '30 days')
                      )
                    ORDER BY disagreement_score DESC, support_weight ASC, created_at ASC
                    LIMIT %s;
                    """,
                    (*scope_param, days_threshold, min_disagreement, limit),
                )
                rows = cur.fetchall()

        results: list[dict[str, Any]] = []
        for row in rows:
            ds = float(row[6]) if row[6] is not None else 0.0
            sw = float(row[5]) if row[5] is not None else 0.0
            mt = row[2] or "fact"
            age_days = int(float(row[8])) if row[8] is not None else None

            reasons: list[str] = []
            if ds >= min_disagreement:
                reasons.append(f"contested (disagreement_score={ds:.2f})")
            if age_days is not None and age_days > days_threshold and sw < 0.5:
                reasons.append(f"old ({age_days}d) with low support ({sw:.2f})")
            if mt in ("opinion", "preference", "lesson", "belief") and age_days is not None and age_days > 30:
                reasons.append(f"volatile type '{mt}' ({age_days}d old)")

            results.append({
                "id": str(row[0]),
                "content": row[1],
                "memory_type": mt,
                "scope": row[3],
                "confidence": float(row[4]) if row[4] is not None else 0.5,
                "support_weight": sw,
                "disagreement_score": ds,
                "created_at": row[7].isoformat() if row[7] is not None else None,
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
        """Find pairs of active atoms with cosine similarity above threshold using pgvector."""
        scope_clause = "AND a.scope = %s AND b.scope = %s" if scope else ""
        scope_param: tuple = (scope, scope) if scope else ()
        threshold_as_distance = 1.0 - similarity_threshold

        with psycopg.connect(self.config.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT
                        a.id AS id_a, a.content AS content_a, a.memory_type AS type_a,
                        a.scope AS scope_a, a.confidence AS conf_a, a.created_at AS created_a,
                        b.id AS id_b, b.content AS content_b, b.memory_type AS type_b,
                        b.scope AS scope_b, b.confidence AS conf_b, b.created_at AS created_b,
                        1 - (a.embedding <=> b.embedding) AS similarity
                    FROM memory_atoms a
                    JOIN memory_atoms b ON a.id < b.id
                    WHERE a.lifecycle_status = 'active'
                      AND b.lifecycle_status = 'active'
                      {scope_clause}
                      AND (a.embedding <=> b.embedding) <= %s
                    ORDER BY similarity DESC
                    LIMIT %s;
                    """,
                    (*scope_param, threshold_as_distance, limit),
                )
                rows = cur.fetchall()

        return [
            {
                "similarity": round(float(row[12]), 4),
                "atom_a": {
                    "id": str(row[0]), "content": row[1], "memory_type": row[2] or "fact",
                    "scope": row[3], "confidence": float(row[4]) if row[4] is not None else 0.5,
                    "created_at": row[5].isoformat() if row[5] is not None else None,
                },
                "atom_b": {
                    "id": str(row[6]), "content": row[7], "memory_type": row[8] or "fact",
                    "scope": row[9], "confidence": float(row[10]) if row[10] is not None else 0.5,
                    "created_at": row[11].isoformat() if row[11] is not None else None,
                },
            }
            for row in rows
        ]

    def list_task_runs_db(
        self,
        scope: str | None = None,
        outcome: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Return recent task_run records."""
        VALID_OUTCOMES = {"success", "partial", "failed"}
        conditions: list[str] = []
        params: list[Any] = []
        if scope is not None:
            conditions.append("scope = %s")
            params.append(scope)
        if outcome is not None and outcome in VALID_OUTCOMES:
            conditions.append("outcome = %s")
            params.append(outcome)
        where_sql = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        params.append(limit)

        with psycopg.connect(self.config.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT id, scope, task_description, model_used,
                           files_changed, outcome, lessons_stored, created_at
                    FROM task_runs {where_sql}
                    ORDER BY created_at DESC LIMIT %s;
                    """,
                    tuple(params),
                )
                rows = cur.fetchall()
        return [
            {
                "id": str(r[0]),
                "scope": r[1],
                "task_description": r[2],
                "model_used": r[3],
                "files_changed": r[4],
                "outcome": r[5],
                "lessons_stored": r[6],
                "created_at": r[7].isoformat() if r[7] else None,
            }
            for r in rows
        ]

    def get_atom_with_signals(self, memory_id: str) -> dict[str, Any] | None:
        """Fetch a single atom with its signals summary."""
        _empty = {"count": 0, "top_sources": [], "most_recent_signal_at": None}

        with psycopg.connect(self.config.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, content, context_summary, memory_type, scope,
                           confidence, importance, support_weight, opposition_weight,
                           disagreement_score, last_recomputed_at, created_at,
                           lifecycle_status, superseded_by_atom_id, lifecycle_reason,
                           retrieval_priority, lifecycle_updated_at
                    FROM memory_atoms WHERE id = %s;
                    """,
                    (memory_id,),
                )
                row = cur.fetchone()
                if row is None:
                    return None

            # signals summary
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*), MAX(created_at) FROM memory_signals WHERE memory_atom_id = %s;",
                    (memory_id,),
                )
                sig_count_row = cur.fetchone()
                sig_count = int(sig_count_row[0]) if sig_count_row else 0
                sig_recent = sig_count_row[1].isoformat() if sig_count_row and sig_count_row[1] else None

                cur.execute(
                    """
                    SELECT source_key FROM (
                        SELECT source_key, MAX(created_at) AS last_seen
                        FROM memory_signals WHERE memory_atom_id = %s
                        GROUP BY source_key ORDER BY last_seen DESC LIMIT 3
                    ) sub;
                    """,
                    (memory_id,),
                )
                top_sources = [r[0] for r in cur.fetchall()]

        sig_summary = {"count": sig_count, "top_sources": top_sources, "most_recent_signal_at": sig_recent}
        atom_id_str = str(row[0])
        ds = float(row[9])
        return {
            "id": atom_id_str,
            "content": row[1],
            "context_summary": row[2],
            "memory_type": row[3],
            "scope": row[4],
            "confidence": float(row[5]),
            "importance": float(row[6]),
            "support_weight": float(row[7]),
            "opposition_weight": float(row[8]),
            "disagreement_score": ds,
            "disagreement_flag": ds >= 0.5,
            "last_recomputed_at": row[10].isoformat() if row[10] else None,
            "created_at": row[11].isoformat() if row[11] else None,
            "lifecycle_status": row[12],
            "superseded_by_atom_id": str(row[13]) if row[13] else None,
            "lifecycle_reason": row[14],
            "retrieval_priority": float(row[15]),
            "lifecycle_updated_at": row[16].isoformat() if row[16] else None,
            "signals_summary": sig_summary,
        }

    def get_atom_signals_db(
        self, memory_atom_id: str, limit: int = 20
    ) -> list[dict[str, Any]]:
        """Return signals for an atom, newest first."""
        with psycopg.connect(self.config.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, memory_atom_id, parent_signal_id, source_key,
                           source_type, source_id, content, context_summary,
                           memory_type, scope, subject, stance, relationship,
                           certainty, intensity, confidence, importance,
                           reconciliation_reason, created_at
                    FROM memory_signals
                    WHERE memory_atom_id = %s
                    ORDER BY created_at DESC LIMIT %s;
                    """,
                    (memory_atom_id, limit),
                )
                rows = cur.fetchall()
        return [
            {
                "id": str(r[0]),
                "memory_atom_id": str(r[1]) if r[1] else None,
                "parent_signal_id": str(r[2]) if r[2] else None,
                "source_key": r[3], "source_type": r[4], "source_id": r[5],
                "content": r[6], "context_summary": r[7],
                "memory_type": r[8], "scope": r[9],
                "subject": r[10], "stance": r[11], "relationship": r[12],
                "certainty": float(r[13]) if r[13] is not None else None,
                "intensity": float(r[14]) if r[14] is not None else None,
                "confidence": float(r[15]) if r[15] is not None else None,
                "importance": float(r[16]) if r[16] is not None else None,
                "reconciliation_reason": r[17],
                "created_at": r[18].isoformat() if r[18] else None,
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
        """Semantic search with signals summary included."""
        embedding = self.ollama.embed_text(query)
        embedding_literal = self._vector_literal(embedding)
        clamped_min_similarity = max(0.0, min(float(min_similarity), 1.0))

        where_clauses: list[str] = ["lifecycle_status != 'archived'"]
        filter_params: list[Any] = []
        if scope:
            where_clauses.append("scope = %s")
            filter_params.append(scope)
        if memory_type:
            where_clauses.append("memory_type = %s")
            filter_params.append(memory_type)
        where_sql = "WHERE " + " AND ".join(where_clauses)
        params: list[Any] = [embedding_literal] + filter_params + [embedding_literal, limit]

        with psycopg.connect(self.config.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT id, content, context_summary, memory_type, scope,
                           confidence, importance,
                           1 - (embedding <=> %s::vector) AS similarity,
                           created_at, support_weight, opposition_weight,
                           disagreement_score, last_recomputed_at,
                           lifecycle_status, superseded_by_atom_id, lifecycle_reason,
                           retrieval_priority, lifecycle_updated_at
                    FROM memory_atoms {where_sql}
                    ORDER BY embedding <=> %s::vector LIMIT %s;
                    """,
                    tuple(params),
                )
                rows = cur.fetchall()

            valid_rows = [(r, float(r[7])) for r in rows if float(r[7]) >= clamped_min_similarity]
            atom_ids = [str(r[0]) for r, _ in valid_rows]

            # signals summary batch
            sigs: dict[str, Any] = {}
            if atom_ids:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT memory_atom_id::text, COUNT(*), MAX(created_at) "
                        "FROM memory_signals WHERE memory_atom_id = ANY(%s::uuid[]) "
                        "GROUP BY memory_atom_id;",
                        (atom_ids,),
                    )
                    for sg in cur.fetchall():
                        sigs[sg[0]] = {
                            "count": int(sg[1]),
                            "most_recent_signal_at": sg[2].isoformat() if sg[2] else None,
                            "top_sources": [],
                        }
                if sigs:
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            SELECT atom_id, source_key FROM (
                                SELECT memory_atom_id::text AS atom_id, source_key,
                                       ROW_NUMBER() OVER (PARTITION BY memory_atom_id
                                           ORDER BY MAX(created_at) DESC NULLS LAST) AS rn
                                FROM memory_signals WHERE memory_atom_id = ANY(%s::uuid[])
                                GROUP BY memory_atom_id, source_key
                            ) sub WHERE rn <= 3 ORDER BY atom_id, rn;
                            """,
                            (atom_ids,),
                        )
                        for sg in cur.fetchall():
                            if sg[0] in sigs:
                                sigs[sg[0]]["top_sources"].append(sg[1])

        _empty = {"count": 0, "top_sources": [], "most_recent_signal_at": None}
        results: list[dict[str, Any]] = []
        for row, similarity in valid_rows:
            ds = float(row[11])
            results.append({
                "id": str(row[0]),
                "content": row[1],
                "context_summary": row[2],
                "memory_type": row[3],
                "scope": row[4],
                "confidence": float(row[5]),
                "importance": float(row[6]),
                "similarity": similarity,
                "created_at": row[8].isoformat() if row[8] else None,
                "support_weight": float(row[9]),
                "opposition_weight": float(row[10]),
                "disagreement_score": ds,
                "last_recomputed_at": row[12].isoformat() if row[12] else None,
                "disagreement_flag": ds >= 0.5,
                "lifecycle_status": row[13],
                "superseded_by_atom_id": str(row[14]) if row[14] else None,
                "lifecycle_reason": row[15],
                "retrieval_priority": float(row[16]),
                "lifecycle_updated_at": row[17].isoformat() if row[17] else None,
                "signals_summary": sigs.get(str(row[0]), _empty),
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
        """Write a conversation turn to the audit tables."""
        import json as _json
        task_summary = (user_message[:200] + "…") if len(user_message) > 200 else user_message

        with psycopg.connect(self.config.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO runtime_context_traces (
                        task_summary, retrieved_atom_ids, used_atom_ids,
                        ignored_atom_ids, context_status, confidence,
                        issues, required_actions, final_action
                    ) VALUES (%s, %s::jsonb, %s::jsonb, %s::jsonb, %s, %s, %s::jsonb, %s::jsonb, %s)
                    RETURNING id;
                    """,
                    (
                        task_summary,
                        _json.dumps(retrieved_atom_ids),
                        _json.dumps(used_atom_ids),
                        _json.dumps([]),
                        context_status,
                        confidence,
                        _json.dumps([]),
                        _json.dumps([]),
                        final_action,
                    ),
                )
                context_trace_id = str(cur.fetchone()[0])

                cur.execute(
                    """
                    INSERT INTO runtime_response_traces (
                        user_message, draft_answer, final_answer,
                        verdict, overstatement_risk, issues, commit_candidates,
                        reasoning, context_trace_id,
                        gap_status, gap_searches, gap_clarifying_question
                    ) VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s::uuid, %s, %s, %s)
                    RETURNING id;
                    """,
                    (
                        (user_message or "")[:1000],
                        (assistant_response or "")[:4000],
                        (assistant_response or "")[:4000],
                        verdict,
                        "none",
                        _json.dumps([]),
                        _json.dumps([]),
                        (f"[{source}] " + (reasoning or ""))[:500],
                        context_trace_id,
                        "resolved",
                        0,
                        None,
                    ),
                )
                response_trace_id = str(cur.fetchone()[0])
            conn.commit()

        return {
            "context_trace_id": context_trace_id,
            "response_trace_id": response_trace_id,
            "status": "logged",
        }

    def get_and_claim_proposal(
        self, proposal_id: str, approval_token: str
    ) -> dict[str, Any]:
        """Look up a proposal, validate token, mark as used. Returns proposal data or error dict."""
        from datetime import datetime, timezone
        now_utc = datetime.now(tz=timezone.utc)

        with psycopg.connect(self.config.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, status, content, context_summary, memory_type, scope,
                           confidence, importance, relationship, reconciliation_reason,
                           matched_memory_ids, approval_token, token_expires_at
                    FROM memory_proposals WHERE id = %s;
                    """,
                    (proposal_id.strip(),),
                )
                row = cur.fetchone()

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
            if p_token_expires_at is None or now_utc > p_token_expires_at:
                with conn.cursor() as cur:
                    cur.execute("UPDATE memory_proposals SET status = 'expired' WHERE id = %s", (p_id,))
                conn.commit()
                return {"error": "approval token has expired. Run `make review-proposals` again."}

            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE memory_proposals SET status = 'used', reviewed_at = now() WHERE id = %s",
                    (p_id,),
                )
            conn.commit()

        return {
            "content": p_content,
            "context_summary": p_context_summary,
            "memory_type": p_memory_type,
            "scope": p_scope,
            "confidence": float(p_confidence),
            "importance": float(p_importance),
            "relationship": p_relationship,
            "reconciliation_reason": p_reconciliation_reason,
            "matched_memory_ids": p_matched_ids_json,
        }


def _pg_atom_row_to_dict(row: tuple) -> dict[str, Any]:
    """Convert a 17-column memory_atoms SELECT row to a dict."""
    ds = float(row[10]) if row[10] is not None else 0.0
    return {
        "id": str(row[0]),
        "content": row[1],
        "context_summary": row[2],
        "memory_type": row[3],
        "scope": row[4],
        "confidence": float(row[5]),
        "importance": float(row[6]),
        "created_at": row[7].isoformat() if row[7] else None,
        "support_weight": float(row[8]) if row[8] is not None else 0.0,
        "opposition_weight": float(row[9]) if row[9] is not None else 0.0,
        "disagreement_score": ds,
        "last_recomputed_at": row[11].isoformat() if row[11] else None,
        "disagreement_flag": ds >= 0.5,
        "lifecycle_status": row[12] or "active",
        "superseded_by_atom_id": str(row[13]) if row[13] else None,
        "lifecycle_reason": row[14],
        "retrieval_priority": float(row[15]) if row[15] is not None else 1.0,
        "lifecycle_updated_at": row[16].isoformat() if row[16] else None,
    }
