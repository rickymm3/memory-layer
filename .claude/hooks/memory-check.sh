#!/bin/bash
# UserPromptSubmit hook — fires before every user prompt.
# If memory_task_context hasn't been called this session, injects a
# mandatory enforcement block. Never stays silent until memory is loaded.
# Uses python3 for JSON parsing — jq is not guaranteed to be present.

INPUT=$(cat)
SESSION_ID=$(echo "$INPUT" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('session_id') or '')" 2>/dev/null)
MARKER="$HOME/.claude/memory-sessions/${SESSION_ID}.initialized"

# Already initialized this session — stay silent
if [ -n "$SESSION_ID" ] && [ -f "$MARKER" ]; then
    exit 0
fi

# Memory not initialized — inject hard enforcement block
cat <<'ENDJSON'
{
  "hookSpecificOutput": {
    "hookEventName": "UserPromptSubmit",
    "additionalContext": "=== MEMORY LAYER ENFORCEMENT ===\n\nmemory_task_context HAS NOT been called this session.\n\nDo NOT answer the user's message yet. Do NOT write code. Do NOT explain anything.\n\nIf you don't know what the user wants or what context applies — say 'I don't know, let me load context first.' Then call:\n\n  memory_task_context(\n    project_scope=\"project:memory-layer\",\n    model_scope=\"model:claude-sonnet-4-6\",\n    task_hint=\"<one sentence describing what user is asking>\"\n  )\n\nThen respond. This is not optional. Guessing without context is how bugs get shipped.\n\n=== END ENFORCEMENT ==="
  }
}
ENDJSON
