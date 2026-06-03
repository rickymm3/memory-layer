#!/usr/bin/env python3
"""
Extract memory candidates from conversation text using qwen3:8b.
- Accepts text as argument or stdin.
- Calls Ollama via app/ollama_client.py.
- Prints strict JSON memory candidates or clear error if invalid.
- Does NOT store or insert anything.
"""
import argparse
import sys
import json
import re
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.llm_provider import get_llm_client


PROPER_NOUNS = ("Ollama", "Postgres", "pgvector", "ComfyUI")
GENERIC_PROJECT_SCOPES = {"project", "user's project", "users project", "this project"}
PROJECT_PHRASES = (
    "this project",
    "the project",
    "memory-layer project",
    "this memory-layer project",
)


def _normalize_scope_value(raw_scope: str) -> str:
    scope = raw_scope.strip().lower().rstrip(".!")
    if scope.startswith("for "):
        scope = scope[4:].strip()
    scope = re.sub(r"\s+", " ", scope)
    return scope


def _is_user_scoped_statement(content: str, memory_type: str) -> bool:
    lowered = content.strip().lower()
    if not lowered.startswith("the user"):
        return False
    return memory_type in {"preference", "opinion", "correction"}

def build_extraction_prompt(conversation: str) -> str:
    conversation_text = conversation.strip()
    return f"""
You are a memory extraction assistant. Your job is to propose durable, reusable memory atoms from a conversation.

Rules:
- Only extract memory from the user's message. The assistant answer is context only and must not be treated as a source of truth. Do not create memory candidates from assistant-generated statements unless the user explicitly confirms, corrects, or instructs something.
- Do not propose candidates that simply restate retrieved memories already provided as context for the assistant answer.
- If the user message is only a question, usually return an empty candidates array.
- Only propose facts, preferences, instructions, corrections, decisions, opinions, relationships, warnings, or temporary_contexts that are durable and reusable.
- Do NOT include one-off questions, temporary chatter, or raw conversation text.
- If nothing is worth storing, return an empty candidates array.
- Do not add reasons or motives that were not explicitly stated by the user.
- Preserve uncertainty words like "maybe", "can", "might", "probably", "for now", and "later" when they appear.
- Do not convert "can be added later" into "will be added later".
- Do not convert "I think" into a firm permanent decision unless the statement is clearly directive.
- Before marking should_store=true, assess whether the candidate captures a genuinely distinct, reusable insight, instruction, or fact. If it is vague, obvious, or something any reasonable assistant would already know without being told, set should_store=false.
- Do not store verbatim quotes. Rewrite accepted candidates as clear, complete, durable standalone statements that express the full intent — as if the original conversation will not be available when the memory is read in future.
- Expand abbreviated or shorthand statements to their full meaning. For example, if the user says "use web search", write a memory that captures what should be done, in what context, and why if stated.
- Do not invent reasons or motives not present in the conversation while rewriting.
- Candidate content must be a complete standalone sentence that can be stored as-is as the source-of-truth memory atom.
- Preserve scope qualifiers when needed for standalone meaning (for example: "For this project", "For this user", "In this repo"). Do not drop them from content.
- Do not rewrite content into lowercase fragments or clause-only snippets.
- context_summary is optional and should be a concise, grammatical, faithful summary of content.
- context_summary is optional and should be a concise, grammatical, faithful summary of content.
- context_summary may be shorter than content and can omit a leading scope qualifier when the underlying meaning remains clear and faithful.
- context_summary must preserve uncertainty words like "for now", "maybe", "can", "might", and "later".
- context_summary must not invent reasons and must not make uncertain statements firmer.
- context_summary must preserve exact proper nouns and casing when present, including: Ollama, Postgres, pgvector, ComfyUI.
- If unsure whether context_summary is faithful, use content as context_summary.
- If a statement says something "can", "might", "maybe", or "could" happen later, treat it as speculative.
- Speculative future possibilities should usually have should_store=false unless the user clearly frames them as a decision, plan, or instruction.
- "For now" decisions can be stored if they affect current project behavior.
- Distinguish clearly between the user, the assistant, named third parties, and hypothetical/example people.
- Do not describe a third-party preference as a "user preference" unless the conversation clearly says that person is the user.
- If a candidate memory is about an unknown third party, lower importance unless the conversation clearly establishes that person, project, or organization as important or recurring.
- High importance should be reserved for memories about the user, the current project, explicit instructions, durable technical decisions, corrections, or recurring entities.
- Third-party facts/preferences should usually have lower importance unless they are clearly relevant to the user's future work.
- Do not mark generic third-party examples as highly important.
- Unknown third-party examples like "Alice prefers dark mode" or "Bob decided to use Postgres" should usually be low importance, around 0.2-0.4, unless Alice/Bob are clearly recurring and relevant.
- For unknown third-party facts, keep should_store=false unless there is clear future usefulness.
- High confidence means the statement was clearly said; it does not mean the memory is important.
- Importance measures future usefulness, not certainty.
- If the text looks like a toy/example sentence, be especially conservative.

Return ONLY strict JSON in this format:
{{
  "candidates": [
    {{
      "content": <string>,
        "context_summary": <string|null>,
      "memory_type": <string: fact|preference|instruction|correction|decision|opinion|relationship|warning|temporary_context>,
      "scope": <string|null>,
      "confidence": <float 0-1>,
      "importance": <float 0-1>,
      "should_store": <bool>,
      "reason": <string>
    }}
    ...
  ]
}}

<conversation>
{conversation_text}
</conversation>
"""


def _mentions_named_third_party(content: str) -> bool:
    content_lower = content.lower()
    if any(phrase in content_lower for phrase in PROJECT_PHRASES):
        return False

    # Heuristic: capitalized entity-like tokens excluding role/project/common terms.
    tokens = re.findall(r"\b[A-Z][a-zA-Z0-9_-]*\b", content)
    ignore = {
        "I",
        "User",
        "Assistant",
        "The",
        "A",
        "An",
        "This",
        "That",
        "For",
        "Project",
        "Memory",
        "Layer",
        "Memory-layer",
        "Ollama",
        "Postgres",
        "Pgvector",
        "ComfyUI",
        "Qwen3",
    }
    for token in tokens:
        if token not in ignore:
            return True
    return False


def _conversation_is_memory_layer_project(conversation: str) -> bool:
    text = conversation.lower()
    markers = (
        "memory-layer",
        "memory layer",
        "memory-layer project",
        "this memory-layer project",
    )
    return any(marker in text for marker in markers)


def _print_over_scope_warnings(data: dict) -> None:
    generic_scopes = {"user_interface", "database", "project", "database_system"}
    candidates = data.get("candidates", [])
    if not isinstance(candidates, list):
        return

    for idx, candidate in enumerate(candidates, start=1):
        if not isinstance(candidate, dict):
            continue
        content = str(candidate.get("content", ""))
        scope = candidate.get("scope")
        if isinstance(scope, str) and scope in generic_scopes and _mentions_named_third_party(content):
            print(
                f"[WARN] Candidate {idx} may be over-scoped: named third party in content with generic scope '{scope}'.",
                file=sys.stderr,
            )


def _parse_model_json_response(response: str) -> dict:
    start = response.find('{')
    end = response.rfind('}')
    if start == -1 or end == -1 or end <= start:
        raise ValueError("No JSON object found in model response.")

    json_str = response[start:end + 1]
    data = json.loads(json_str)
    if not isinstance(data, dict) or "candidates" not in data:
        raise ValueError("JSON does not contain 'candidates' array.")

    candidates = data.get("candidates", [])
    if not isinstance(candidates, list):
        raise ValueError("'candidates' is not an array.")

    return data


def _summary_preserves_required_proper_nouns(content: str, summary: str) -> bool:
    content_lower = content.lower()
    for noun in PROPER_NOUNS:
        if noun.lower() in content_lower and noun not in summary:
            return False
    return True


def _sanitize_candidates(data: dict, conversation: str) -> dict:
    raw_candidates = data.get("candidates", [])
    sanitized: list[dict] = []
    repo_name = Path(__file__).resolve().parent.parent.name.lower()
    is_memory_layer_context = _conversation_is_memory_layer_project(conversation) or (repo_name == "memory-layer")

    for candidate in raw_candidates:
        if not isinstance(candidate, dict):
            continue

        content = str(candidate.get("content", "")).strip()
        if not content:
            continue

        summary_val = candidate.get("context_summary")
        summary = str(summary_val).strip() if summary_val is not None else ""

        if not summary:
            summary = content

        # Safety fallback: if summary may have drifted from required proper nouns, use content.
        if not _summary_preserves_required_proper_nouns(content, summary):
            summary = content

        memory_type = str(candidate.get("memory_type", "")).strip().lower()

        scope_val = candidate.get("scope")
        if isinstance(scope_val, str):
            normalized_scope = _normalize_scope_value(scope_val)

            project_scope_aliases = {
                "project",
                "this project",
                "the project",
                "memory-layer project",
                "this memory-layer project",
                "the memory-layer project",
            }
            user_scope_aliases = {
                "user",
                "this user",
                "the user",
                "user preference",
                "user preferences",
            }

            if is_memory_layer_context and (
                normalized_scope in GENERIC_PROJECT_SCOPES
                or normalized_scope in project_scope_aliases
            ):
                candidate["scope"] = "project:memory-layer"
            elif normalized_scope in user_scope_aliases:
                candidate["scope"] = "user"
            elif normalized_scope == "global" and _is_user_scoped_statement(content, memory_type):
                candidate["scope"] = "user"
            else:
                candidate["scope"] = normalized_scope
        elif _is_user_scoped_statement(content, memory_type):
            candidate["scope"] = "user"

        candidate["content"] = content
        candidate["context_summary"] = summary
        sanitized.append(candidate)

    return {"candidates": sanitized}


def extract_candidates(conversation: str, include_rejected: bool = False) -> dict:
    if not conversation.strip():
        raise ValueError("No conversation text provided.")

    prompt = build_extraction_prompt(conversation)
    response = get_llm_client().generate_response(prompt)
    data = _sanitize_candidates(_parse_model_json_response(response), conversation=conversation)

    if not include_rejected:
        filtered = [
            candidate
            for candidate in data.get("candidates", [])
            if isinstance(candidate, dict) and bool(candidate.get("should_store"))
        ]
        data = {"candidates": filtered}

    _print_over_scope_warnings(data)
    return data

def build_reflection_prompt(user_msg: str, thinking: str, answer: str) -> str:
    """Build a full-turn reflection prompt for post-turn memory extraction.

    Unlike build_extraction_prompt (user message only), this covers the full turn:
    user intent, LLM reasoning, and LLM answer are all valid memory sources.
    The LLM acts as an active judge of what is worth storing permanently.
    """
    parts = [f"User: {user_msg.strip()[:1000]}"]
    if thinking.strip():
        parts.append(f"Assistant reasoning:\n{thinking.strip()[:2000]}")
    parts.append(f"Assistant answer: {answer.strip()[:1500]}")
    turn_text = "\n\n".join(parts)

    return f"""You are a memory curation assistant. Assess this completed assistant exchange and decide what is worth storing as a durable, reusable memory atom.

Sources you may extract from:
- The user's message: explicit preferences, instructions, corrections, decisions, opinions.
- The assistant's reasoning: patterns discovered, derived knowledge, non-obvious insights, corrections to prior beliefs, methodological decisions made during solving.
- The assistant's answer: conclusions reached, decisions taken, information that was novel and required reasoning to arrive at.

Rules:
- Only store content that is durable and reusable in future turns. Do not store one-off answers, generic factual knowledge the model already has, or trivial Q&A.
- Do not store reasoning steps valid only for this specific task instance — only store insights or patterns that transfer to future exchanges.
- Do not store anything obviously known without this conversation.
- Do not invent reasons or expand on what was stated.
- Preserve uncertainty words (maybe, might, probably, for now, can, later).
- High confidence means it was clearly stated or derived; importance measures future usefulness.
- If nothing in this turn is durably worth storing, return an empty candidates array.
- Candidate content must be a complete standalone sentence.
- Do not store verbatim quotes — rewrite as durable standalone statements.
- context_summary must be faithful and preserve proper nouns and uncertainty words.
- Preserve scope qualifiers in content when needed for standalone meaning.

Return ONLY strict JSON:
{{
  "candidates": [
    {{
      "content": <string>,
      "context_summary": <string|null>,
      "memory_type": <string: fact|preference|instruction|correction|decision|opinion|relationship|warning|temporary_context>,
      "scope": <string|null>,
      "confidence": <float 0-1>,
      "importance": <float 0-1>,
      "should_store": <bool>,
      "reason": <string>
    }}
  ]
}}

<turn>
{turn_text}
</turn>
"""


def extract_candidates_from_turn(
    user_msg: str,
    thinking: str,
    answer: str,
    include_rejected: bool = False,
) -> dict:
    """Extract memory candidates from a completed assistant turn (full scope).

    Unlike extract_candidates (user-message-only), this processes the full turn:
    user intent, LLM reasoning, and LLM answer are all valid sources.
    """
    if not user_msg.strip() and not answer.strip():
        raise ValueError("At least one of user_msg or answer must be non-empty.")

    prompt = build_reflection_prompt(user_msg, thinking, answer)
    response = get_llm_client().generate_response(prompt)
    data = _sanitize_candidates(
        _parse_model_json_response(response),
        conversation=user_msg,
    )

    if not include_rejected:
        filtered = [
            c for c in data.get("candidates", [])
            if isinstance(c, dict) and bool(c.get("should_store"))
        ]
        data = {"candidates": filtered}

    _print_over_scope_warnings(data)
    return data


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract memory candidates from conversation text.")
    parser.add_argument("text", nargs="?", help="Conversation text (or use stdin)")
    parser.add_argument(
        "--include-rejected",
        action="store_true",
        help="Include candidates where should_store is false",
    )
    args = parser.parse_args()

    if args.text:
        conversation = args.text
    else:
        conversation = sys.stdin.read()

    try:
        data = extract_candidates(conversation, include_rejected=args.include_rejected)
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return 0
    except Exception as e:
        print("[ERROR] Failed to extract/parse strict JSON memory candidates.", file=sys.stderr)
        print(f"Error: {e}", file=sys.stderr)
        return 2

if __name__ == "__main__":
    raise SystemExit(main())
