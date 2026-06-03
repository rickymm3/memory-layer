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
        root_dir = Path(__file__).resolve().parent.parent
        init_sql_path = root_dir / "db" / "init.sql"
        sql = init_sql_path.read_text(encoding="utf-8")

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
    ) -> str:
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
                cur.execute(
                    f"""
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
                    WHERE 1 - (embedding <=> %s::vector) >= %s
                      AND (lifecycle_status IS NULL
                           OR lifecycle_status NOT IN ('superseded', 'deprecated', 'archived'))
                      {scope_clause}
                    ORDER BY embedding <=> %s::vector
                    LIMIT %s;
                    """,
                    (embedding_literal, embedding_literal, threshold, *scope_params, embedding_literal, limit),
                )
                rows = cur.fetchall()

        results: list[dict[str, Any]] = []
        for row in rows:
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
                    "created_at": row[8].isoformat() if row[8] is not None else None,
                    "lifecycle_status": row[9] or "active",
                    "support_weight": float(row[10]) if row[10] is not None else 0.0,
                    "opposition_weight": float(row[11]) if row[11] is not None else 0.0,
                    "disagreement_score": float(row[12]) if row[12] is not None else 0.0,
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
