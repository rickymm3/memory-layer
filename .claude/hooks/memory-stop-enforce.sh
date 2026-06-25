#!/bin/bash
# Stop hook — fires when Claude finishes a response turn.
# If the session is initialized (memory_task_context was called) but
# memory_store_auto was NOT called this turn, block the turn from ending
# and force the model to evaluate whether a memory write is needed.
# Gives up after 2 blocks to prevent infinite loops.

INPUT=$(cat)
SESSION_ID=$(echo "$INPUT" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('session_id') or '')" 2>/dev/null)

if [ -z "$SESSION_ID" ]; then
    exit 0
fi

MARKER_DIR="$HOME/.claude/memory-sessions"
INIT_MARKER="${MARKER_DIR}/${SESSION_ID}.initialized"
WRITE_MARKER="${MARKER_DIR}/${SESSION_ID}.wrote-this-turn"
BLOCK_COUNT_FILE="${MARKER_DIR}/${SESSION_ID}.stop-block-count"

# Session not initialized — nothing to enforce yet
if [ ! -f "$INIT_MARKER" ]; then
    exit 0
fi

# Memory was written this turn — clear markers and allow stop
if [ -f "$WRITE_MARKER" ]; then
    rm -f "$WRITE_MARKER"
    rm -f "$BLOCK_COUNT_FILE"
    exit 0
fi

# Already blocked twice this turn — give up to prevent infinite loop
count=0
if [ -f "$BLOCK_COUNT_FILE" ]; then
    count=$(cat "$BLOCK_COUNT_FILE" 2>/dev/null || echo 0)
fi

if [ "$count" -ge 2 ]; then
    rm -f "$BLOCK_COUNT_FILE"
    exit 0
fi

# Increment block count and block the stop
echo $((count + 1)) > "$BLOCK_COUNT_FILE"

python3 -c "
import json
print(json.dumps({
    'decision': 'block',
    'reason': (
        'CLAUDE.md rule violation: memory_store_auto was not called this turn.\n\n'
        'Review this turn. If the user expressed a preference, correction, decision, '
        'instruction, or if you observed a behavioral pattern — call memory_store_auto now.\n\n'
        'Scope discipline: project facts -> scope=\"project:memory-layer\", '
        'model behavior observations -> scope=\"model:claude-sonnet-4-6\", '
        'user preferences -> scope=\"user\".\n\n'
        'If this turn was genuinely trivial (one-word reply, no preferences expressed, '
        'pure mechanical output) — call memory_store_auto with '
        'memory_type=\"observation\", relationship=\"new\", scope=\"model:claude-sonnet-4-6\" '
        'and content describing why this turn had nothing to store. '
        'That write satisfies the requirement and clears this block.'
    )
}))
"
exit 2
