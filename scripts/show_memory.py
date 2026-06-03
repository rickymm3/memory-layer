#!/usr/bin/env python3
"""Inspect a single memory atom and its linked signals.

Usage:
    .venv/bin/python scripts/show_memory.py <memory_atom_id>

Prints the atom's content, aggregate fields, and a summary of all linked
signals — showing provenance and why the atom has its current confidence
and disagreement_score.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()

from mcp_server.tools.get import get_memory_by_id
from mcp_server.tools.get_signals import get_atom_signals

AGREEING = frozenset({"new", "reinforcement", "refinement"})
CONFLICTING = frozenset({"conflict", "opinion_change"})


def _flag(disagreement_score: float) -> str:
    if disagreement_score >= 0.5:
        return "  ⚑ CONTESTED (disagreement_score >= 0.5)"
    if disagreement_score > 0.0:
        return "  (some disagreement)"
    return ""


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] in ("-h", "--help"):
        print(f"Usage: {Path(sys.argv[0]).name} <memory_atom_id>")
        return 1

    atom_id = sys.argv[1].strip()

    atom = get_memory_by_id(atom_id)
    if atom is None:
        print(f"[ERROR] No atom found with id: {atom_id}", file=sys.stderr)
        return 1

    # ── atom summary ──────────────────────────────────────────────────────────
    print("=" * 70)
    print(f"Memory Atom: {atom['id']}")
    print("=" * 70)
    print(f"  type/scope:     {atom['memory_type']} / {atom.get('scope') or 'global'}")
    print(f"  content:        {atom['content']}")
    summary = atom.get("context_summary") or ""
    if summary and summary != atom["content"]:
        print(f"  context_summary:{summary}")
    print()
    print("  Aggregate fields:")
    disagreement_score = atom.get("disagreement_score", 0.0)
    flag = _flag(disagreement_score)
    print(f"    confidence:        {atom.get('confidence', 0.0):.4f}")
    print(f"    support_weight:    {atom.get('support_weight', 0.0):.4f}")
    print(f"    opposition_weight: {atom.get('opposition_weight', 0.0):.4f}")
    print(f"    disagreement_score:{disagreement_score:.4f}{flag}")
    last_recomputed = atom.get("last_recomputed_at") or "never"
    print(f"    last_recomputed_at:{last_recomputed[:19] if last_recomputed != 'never' else last_recomputed}")
    print(f"    created_at:        {(atom.get('created_at') or '')[:19]}")

    # ── signals ───────────────────────────────────────────────────────────────
    signals = get_atom_signals(atom_id, limit=100)
    print()
    print(f"  Linked signals: {len(signals)}")

    if not signals:
        print("    (none)")
    else:
        for i, sig in enumerate(signals, start=1):
            rel = sig.get("relationship") or "?"
            if rel in AGREEING:
                kind = "SUPPORT"
            elif rel in CONFLICTING:
                kind = "CONFLICT"
            else:
                kind = "neutral"
            conf = sig.get("confidence")
            conf_str = f"{conf:.3f}" if conf is not None else "n/a"
            source = sig.get("source_key") or "?"
            created = (sig.get("created_at") or "")[:19]
            content_preview = (sig.get("content") or "")[:60]
            print(
                f"    {i:2}. [{kind:7}] rel={rel:<16} conf={conf_str}"
                f"  src={source}  {created}"
            )
            print(f"        {content_preview}")
            reason = sig.get("reconciliation_reason") or ""
            if reason:
                print(f"        reason: {reason[:70]}")

    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
