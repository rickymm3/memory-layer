#!/usr/bin/env python3
"""Display model-scope atoms grouped by category.

Usage:
    python scripts/model_report.py                          # all model:* scopes
    python scripts/model_report.py --model qwen3-8b        # specific model
    python scripts/model_report.py --model qwen3-8b --all  # include non-active
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import psycopg
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_SEP = "═" * 66
_SEP2 = "─" * 66

# memory_type → display category
_CATEGORY = {
    "instruction": "Prompt Adaptations",
    "fact": "Model Observations",
    "decision": "Model Observations",
}
_CATEGORY_ORDER = ["Model Observations", "Prompt Adaptations", "General"]

DEFAULT_DATABASE_URL = (
    "postgresql://memory:memory_dev_password@localhost:5432/memory_layer_development"
)


def _format_float(v: float | None) -> str:
    return f"{v:.2f}" if v is not None else "n/a"


def main() -> int:
    load_dotenv()

    parser = argparse.ArgumentParser(description="Display model-scope memory atoms by category.")
    parser.add_argument(
        "--model",
        default=None,
        metavar="NAME",
        help="Model name (without 'model:' prefix, e.g. qwen3-8b). Omit to show all.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Include non-active atoms (superseded, deprecated, archived, contested).",
    )
    args = parser.parse_args()

    database_url = os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL).strip()
    try:
        conn = psycopg.connect(database_url)
    except Exception as exc:
        print(f"[ERROR] Could not connect to database: {exc}", file=sys.stderr)
        return 1

    # Build scope filter
    if args.model:
        scope_filter = f"model:{args.model}"
        scope_clause = "AND scope = %s"
        scope_params: list = [scope_filter]
    else:
        scope_clause = "AND scope LIKE %s"
        scope_params = ["model:%"]

    lifecycle_clause = "" if args.all else "AND lifecycle_status = 'active'"

    rows = conn.execute(
        f"""
        SELECT id, scope, content, context_summary, memory_type,
               confidence, importance, lifecycle_status, created_at
        FROM memory_atoms
        WHERE 1=1
          {scope_clause}
          {lifecycle_clause}
        ORDER BY scope, importance DESC NULLS LAST, confidence DESC NULLS LAST
        """,
        scope_params,
    ).fetchall()

    if not rows:
        scope_label = f"model:{args.model}" if args.model else "model:*"
        active_label = "" if args.all else " active"
        print(f"No{active_label} atoms found for {scope_label}.")
        return 0

    # Group by scope then category
    by_scope: dict[str, dict[str, list]] = {}
    for atom_id, scope, content, ctx_summary, mtype, confidence, importance, status, created_at in rows:
        by_scope.setdefault(scope, {cat: [] for cat in _CATEGORY_ORDER})
        cat = _CATEGORY.get(mtype or "", "General")
        by_scope[scope][cat].append(
            {
                "id": atom_id,
                "content": content,
                "context_summary": ctx_summary,
                "memory_type": mtype,
                "confidence": confidence,
                "importance": importance,
                "status": status,
                "created_at": created_at,
            }
        )

    total = len(rows)
    print()
    print(_SEP)
    scope_count = len(by_scope)
    print(
        f"  MODEL REPORT  —  {total} atom{'s' if total != 1 else ''}"
        f" across {scope_count} scope{'s' if scope_count != 1 else ''}"
    )
    print(_SEP)

    for scope, categories in sorted(by_scope.items()):
        scope_atoms = sum(len(v) for v in categories.values())
        print(f"\n  Scope: {scope}  ({scope_atoms} atom{'s' if scope_atoms != 1 else ''})")
        print(_SEP2)

        any_printed = False
        for cat in _CATEGORY_ORDER:
            atoms = categories[cat]
            if not atoms:
                continue
            any_printed = True
            print(f"\n  [{cat}]")
            for a in atoms:
                status_flag = f"  [{a['status']}]" if a["status"] != "active" else ""
                type_label = f"({a['memory_type']})" if a["memory_type"] else "(untyped)"
                print(f"\n    {str(a['id'])[:8]}  {type_label}{status_flag}")
                # Wrap content at 62 chars
                content = a["content"]
                words = content.split()
                line = "    "
                for word in words:
                    if len(line) + len(word) + 1 > 66:
                        print(line)
                        line = "    " + word + " "
                    else:
                        line += word + " "
                if line.strip():
                    print(line)
                # Show injection string if present and differs from content
                if a.get("context_summary") and a["context_summary"] != a["content"]:
                    print(f"    INJECT → {a['context_summary']}")
                print(
                    f"    conf={_format_float(a['confidence'])}  "
                    f"imp={_format_float(a['importance'])}"
                )

        if not any_printed:
            print("  (no atoms)")

        print()
        print(_SEP2)

    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
