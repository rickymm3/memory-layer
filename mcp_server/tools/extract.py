from __future__ import annotations

from typing import Any

from scripts.extract_memory_candidates import extract_candidates


def extract_memory_candidates(
    text: str,
    context: str | None = None,
) -> list[dict[str, Any]]:
    """Extract memory candidate atoms from a text snippet without storing anything.

    Runs the extraction LLM pipeline and returns candidates that the write
    policy considers worth storing. Does not write to the database. Use this
    as the first step before calling memory_reconcile_candidate on each result.

    Args:
        text: The text to extract memory candidates from. Only user-authored
              content should be passed here; assistant output is context only.
        context: Optional surrounding context (e.g. assistant reply or retrieved
                 memories) to help the extractor interpret the text. Appended
                 after the main text, clearly labelled as context only.
    """
    if not text or not text.strip():
        return []

    if context and context.strip():
        full_input = (
            f"{text.strip()}\n\n"
            f"<context_only>\n{context.strip()}\n</context_only>"
        )
    else:
        full_input = text.strip()

    result = extract_candidates(full_input, include_rejected=False)
    return result.get("candidates", [])
