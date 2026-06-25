#!/bin/bash
# PostToolUse hook — fires when memory_store_auto completes.
# Sets a per-turn marker so the Stop hook knows memory was written.
# Clears any stop-block-count so the Stop hook resets for next turn.

INPUT=$(cat)
SESSION_ID=$(echo "$INPUT" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('session_id') or '')" 2>/dev/null)

if [ -n "$SESSION_ID" ]; then
    mkdir -p "$HOME/.claude/memory-sessions"
    touch "$HOME/.claude/memory-sessions/${SESSION_ID}.wrote-this-turn"
    rm -f "$HOME/.claude/memory-sessions/${SESSION_ID}.stop-block-count"
fi
exit 0
