#!/bin/bash
# PreCompact hook — fires before Claude Code compacts the conversation.
#
# Injects a structured MEMORY LAYER CONTEXT block into the pre-compaction
# context so the compactor's summary preserves memory-relevant facts.
#
# Also handles PostCompact (fired via SessionStart matcher="compact"):
# that re-runs memory_task_context automatically via the existing SessionStart hook.
#
# If PreCompact doesn't support additionalContext injection, this exits 0
# harmlessly — the SessionStart hook handles post-compact memory reload.

INPUT=$(cat)
SESSION_ID=$(echo "$INPUT" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('session_id') or '')" 2>/dev/null)
PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(pwd)}"

# Load .env for DATABASE_URL
if [ -f "$PROJECT_DIR/.env" ]; then
    set -a
    source "$PROJECT_DIR/.env"
    set +a
fi

# Fetch top 10 high-confidence atoms for this project as a compact block
ATOMS=$("$PROJECT_DIR/.venv/bin/python3" - <<'PYEOF' 2>/dev/null
import os, sys
try:
    import psycopg
    db = os.environ.get("DATABASE_URL", "")
    if not db:
        sys.exit(0)
    with psycopg.connect(db) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT memory_type, confidence, content
                FROM memory_atoms
                WHERE lifecycle_status = 'active'
                  AND scope = 'project:memory-layer'
                ORDER BY (confidence * importance) DESC
                LIMIT 10
            """)
            rows = cur.fetchall()
    lines = []
    for mtype, conf, content in rows:
        lines.append(f"[{mtype}] ({conf:.2f}) {content}")
    print("\n".join(lines))
except Exception:
    pass
PYEOF
)

if [ -z "$ATOMS" ]; then
    exit 0
fi

# Output additionalContext for the compactor to include in its summary.
# Escape the atoms block as a JSON string value.
ESCAPED_ATOMS=$(echo "$ATOMS" | python3 -c "import json,sys; print(json.dumps(sys.stdin.read()))" 2>/dev/null || echo '""')

python3 - <<PYEOF
import json
context = "## MEMORY LAYER — PRE-COMPACTION CONTEXT\\nThe following project memory atoms should be preserved in the compaction summary:\\n\\n" + ${ESCAPED_ATOMS} + "\\n\\nThese represent durable decisions and constraints that outlive any single session."
print(json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "PreCompact",
        "additionalContext": context
    }
}))
PYEOF
