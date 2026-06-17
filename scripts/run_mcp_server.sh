#!/usr/bin/env bash
# Wrapper for the memory-layer MCP server.
# Sources .env so secrets stay out of .mcp.json (which may be committed to git).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_ROOT"

if [[ -f "$PROJECT_ROOT/.env" ]]; then
    set -a
    source "$PROJECT_ROOT/.env"
    set +a
fi

exec "$PROJECT_ROOT/.venv/bin/python" -m mcp_server.server
