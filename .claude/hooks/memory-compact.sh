#!/bin/bash
# PreCompact hook — fires before Claude Code compacts the conversation.
# Injects top atoms (by composite score, token-budgeted) so the compactor
# summary preserves durable memory-layer facts.
# PostCompact is handled by the SessionStart hook (matcher="compact").

INPUT=$(cat)
PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(pwd)}"

if [ -f "$PROJECT_DIR/.env" ]; then
    set -a
    source "$PROJECT_DIR/.env"
    set +a
fi

ATOMS=$("$PROJECT_DIR/.venv/bin/python3" - <<'PYEOF' 2>/dev/null
import os, sys

TOKEN_BUDGET = 600  # max tokens for pre-compaction injection

try:
    import psycopg
    db = os.environ.get("DATABASE_URL", "")
    if not db:
        sys.exit(0)
    with psycopg.connect(db) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT memory_type, confidence, importance, support_weight, content
                FROM memory_atoms
                WHERE lifecycle_status IN ('active', 'belief')
                  AND scope = 'project:memory-layer'
                ORDER BY (confidence * importance * (1.0 + COALESCE(support_weight, 0) * 0.5)) DESC
                LIMIT 20
            """)
            rows = cur.fetchall()

    lines = []
    budget = 0
    for mtype, conf, importance, support_weight, content in rows:
        cost = max(1, len(content) // 4)
        if budget + cost > TOKEN_BUDGET:
            break
        budget += cost
        lines.append(f"[{mtype}] ({float(conf):.2f}) {content}")
    print("\n".join(lines))
except Exception:
    pass
PYEOF
)

if [ -z "$ATOMS" ]; then
    exit 0
fi

ESCAPED_ATOMS=$(echo "$ATOMS" | python3 -c "import json,sys; print(json.dumps(sys.stdin.read()))" 2>/dev/null || echo '""')

python3 - <<PYEOF
import json
context = (
    "## MEMORY — PRE-COMPACTION CONTEXT\n"
    "Preserve these project atoms in the compaction summary:\n\n"
    + ${ESCAPED_ATOMS}
)
print(json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "PreCompact",
        "additionalContext": context
    }
}))
PYEOF
