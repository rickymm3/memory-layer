from __future__ import annotations

from typing import Any

from app.reflection import run_turn_reflection


def reflect_turn(
    user_msg: str,
    answer: str,
    thinking: str = "",
    scope: str | None = None,
) -> dict[str, Any]:
    """Run full-turn reflection and commit durable insights to memory.

    Delegates to app.reflection.run_turn_reflection — single source of truth
    shared with the local chat pipeline (_post_turn_reflection in chat.py).

    Args:
        user_msg: The user's message for this turn.
        answer: The assistant's final answer (think blocks already stripped).
        thinking: Raw chain-of-thought text from <think> blocks, if any.
        scope: Optional scope override for all committed atoms.
    """
    return run_turn_reflection(
        user_msg=user_msg,
        thinking=thinking,
        answer=answer,
        scope=scope,
    )
