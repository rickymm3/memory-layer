from __future__ import annotations

import json
from typing import Any

from app.llm_provider import LLMProvider
from app.memory_store import MemoryStore


ALLOWED_RELATIONSHIPS = {
    "duplicate",
    "reinforcement",
    "refinement",
    "conflict",
    "opinion_change",
    "new",
}

ALLOWED_ACTIONS = {"skip", "store_new", "ask_user", "update_existing"}


def _extract_json_object(text: str) -> dict[str, Any] | None:
    text = text.strip()
    if not text:
        return None

    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None

    try:
        parsed = json.loads(text[start : end + 1])
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        return None

    return None


def _sanitize_reconciliation(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {
            "relationship": "new",
            "matched_memory_ids": [],
            "reason": "Reconciliation parser fallback: invalid model JSON.",
            "recommended_action": "ask_user",
        }

    relationship = str(payload.get("relationship", "new")).strip().lower()
    if relationship not in ALLOWED_RELATIONSHIPS:
        relationship = "new"

    matched_ids_raw = payload.get("matched_memory_ids", [])
    matched_ids: list[str] = []
    if isinstance(matched_ids_raw, list):
        for value in matched_ids_raw:
            text = str(value).strip()
            if text:
                matched_ids.append(text)

    reason = str(payload.get("reason", "")).strip() or "No reason provided."

    recommended_action = str(payload.get("recommended_action", "")).strip().lower()
    if recommended_action not in ALLOWED_ACTIONS:
        recommended_action = "ask_user"

    return {
        "relationship": relationship,
        "matched_memory_ids": matched_ids,
        "reason": reason,
        "recommended_action": recommended_action,
    }


def _build_reconciliation_prompt(
    content: str,
    memory_type: str,
    scope: str | None,
    related_memories: list[dict[str, Any]],
) -> str:
    related_payload: list[dict[str, Any]] = []
    for memory in related_memories:
        related_payload.append(
            {
                "id": memory.get("id"),
                "memory_type": memory.get("memory_type"),
                "scope": memory.get("scope"),
                "content": memory.get("content"),
                "context_summary": memory.get("context_summary"),
                "created_at": memory.get("created_at"),
                "similarity": memory.get("similarity"),
            }
        )

    return (
        "You are reconciling a new memory candidate against existing stored memories.\n"
        "Embeddings only indicate relatedness, not truth or duplication.\n"
        "Classify the relationship between the candidate and retrieved existing memories.\n\n"
        "Allowed relationship values:\n"
        "- duplicate: same meaning, no meaningful new information\n"
        "- reinforcement: same meaning/direction, repeated or stronger phrasing\n"
        "- refinement: compatible but more specific or clearer\n"
        "- conflict: contradicts an existing fact/instruction/decision\n"
        "- opinion_change: contradicts or shifts previous opinion/preference/stance\n"
        "- new: no meaningful related existing memory\n\n"
        "Output STRICT JSON ONLY with this schema:\n"
        "{\n"
        "  \"relationship\": \"duplicate\" | \"reinforcement\" | \"refinement\" | \"conflict\" | \"opinion_change\" | \"new\",\n"
        "  \"matched_memory_ids\": [\"...\"],\n"
        "  \"reason\": \"...\",\n"
        "  \"recommended_action\": \"skip\" | \"store_new\" | \"ask_user\" | \"update_existing\"\n"
        "}\n"
        "Do not include markdown, explanation, or extra keys.\n\n"
        "Candidate:\n"
        f"{json.dumps({'content': content, 'memory_type': memory_type, 'scope': scope}, ensure_ascii=False, indent=2)}\n\n"
        "Retrieved existing memories:\n"
        f"{json.dumps(related_payload, ensure_ascii=False, indent=2)}\n"
    )


class CandidateReconciler:
    def __init__(self, store: MemoryStore, ollama: LLMProvider) -> None:
        self.store = store
        self.ollama = ollama

    def reconcile(
        self,
        content: str,
        memory_type: str,
        scope: str | None,
        retrieve_limit: int,
    ) -> dict[str, Any]:
        exact_match = self.store.find_exact_content_match(content)
        if exact_match:
            return {
                "relationship": "duplicate",
                "matched_memory_ids": [exact_match["id"]],
                "reason": "Exact normalized content match with existing memory.",
                "recommended_action": "skip",
                "related_memories": [exact_match],
                "raw_json": {
                    "relationship": "duplicate",
                    "matched_memory_ids": [exact_match["id"]],
                    "reason": "Exact normalized content match with existing memory.",
                    "recommended_action": "skip",
                },
            }

        related_memories = self.store.retrieve_memories(content, limit=retrieve_limit)
        prompt = _build_reconciliation_prompt(
            content=content,
            memory_type=memory_type,
            scope=scope,
            related_memories=related_memories,
        )
        response = self.ollama.generate_response(prompt, json_mode=True)
        parsed = _extract_json_object(response)
        result = _sanitize_reconciliation(parsed)
        result["related_memories"] = related_memories
        result["raw_json"] = parsed if isinstance(parsed, dict) else {"raw_response": response}
        return result