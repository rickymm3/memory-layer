#!/bin/bash
# Stop hook — fires when Claude finishes a response turn.
#
# PRIMARY path: automatically extract memories from the last turn by reading
# the session JSONL and running the extraction pipeline. No model cooperation
# needed — this runs as a background process and writes atoms silently.
#
# FALLBACK path: if the session is initialized but model didn't write AND
# auto-extraction also produced nothing, block once and ask the model to
# evaluate the turn manually (2-retry cap).

INPUT=$(cat)
SESSION_ID=$(echo "$INPUT" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('session_id') or '')" 2>/dev/null)

if [ -z "$SESSION_ID" ]; then
    exit 0
fi

MARKER_DIR="$HOME/.claude/memory-sessions"
INIT_MARKER="${MARKER_DIR}/${SESSION_ID}.initialized"
WRITE_MARKER="${MARKER_DIR}/${SESSION_ID}.wrote-this-turn"
BLOCK_COUNT_FILE="${MARKER_DIR}/${SESSION_ID}.stop-block-count"
PROJECT_DIR="${CLAUDE_PROJECT_DIR:-}"

# ── Auto-extraction ───────────────────────────────────────────────────────────
# Always attempt extraction from the session JSONL regardless of write marker.
# Runs in background so it doesn't delay the turn.
if [ -n "$PROJECT_DIR" ] && [ -f "$PROJECT_DIR/.env" ]; then
    JSONL_PATH="$HOME/.claude/projects/-home-ricky-memory-layer/${SESSION_ID}.jsonl"
    if [ -f "$JSONL_PATH" ]; then
        "$PROJECT_DIR/.venv/bin/python3" "$PROJECT_DIR/scripts/auto_extract_turn.py" \
            "$JSONL_PATH" --user "rickymm3" >> /tmp/memory-auto-extract.log 2>&1 &
        # Mark write so the fallback block doesn't fire for this turn
        touch "${MARKER_DIR}/${SESSION_ID}.wrote-this-turn"
    fi
fi

# ── Fallback: model-cooperation block ────────────────────────────────────────
# Only fires if session was initialized but JSONL auto-extraction couldn't run
# (e.g. project dir not set, JSONL missing) AND model didn't write either.

if [ ! -f "$INIT_MARKER" ]; then
    exit 0
fi

if [ -f "$WRITE_MARKER" ]; then
    rm -f "$WRITE_MARKER"
    rm -f "$BLOCK_COUNT_FILE"
    exit 0
fi

# Check retry cap
count=0
if [ -f "$BLOCK_COUNT_FILE" ]; then
    count=$(cat "$BLOCK_COUNT_FILE" 2>/dev/null || echo 0)
fi

if [ "$count" -ge 2 ]; then
    rm -f "$BLOCK_COUNT_FILE"
    exit 0
fi

echo $((count + 1)) > "$BLOCK_COUNT_FILE"

python3 -c "
import json
print(json.dumps({
    'decision': 'block',
    'reason': (
        'Auto-extraction could not run (JSONL not found or project dir missing). '
        'Call memory_store_auto manually if this turn contained preferences, '
        'corrections, decisions, or instructions. '
        'Scope: project facts -> \"project:memory-layer\", '
        'model observations -> \"model:claude-sonnet-4-6\", '
        'user preferences -> \"user\".'
    )
}))
"
exit 2
