#!/bin/bash
# Writes a per-session marker file so memory-check.sh knows memory_task_context
# has been called. Reads session_id from hook stdin JSON. Idempotent.
# Uses python3 for JSON parsing — jq is not guaranteed to be present.

INPUT=$(cat)
SESSION_ID=$(echo "$INPUT" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('session_id') or '')" 2>/dev/null)

if [ -n "$SESSION_ID" ]; then
    mkdir -p "$HOME/.claude/memory-sessions"
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$HOME/.claude/memory-sessions/${SESSION_ID}.initialized"
fi

exit 0
