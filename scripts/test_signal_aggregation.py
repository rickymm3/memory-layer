#!/usr/bin/env python3
"""Phase 4 signal-aggregation tests.

Structured in three parts:
  Part 1 — Unit tests for app.signal_aggregator.compute_atom_weights (no DB).
            Tests exact formula values with known inputs.
  Part 2 — Integration tests against a live DB.
            Tests auto-recompute on write, full DB roundtrip, and the effect
            of adding a conflicting signal to an existing atom.
  Part 3 — CLI smoke test: make recompute-weights runs and exits 0.

All DB rows created in Part 2 are deleted in a finally block.

Usage:
    .venv/bin/python scripts/test_signal_aggregation.py
"""
from __future__ import annotations

import math
import secrets
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()

import psycopg

from app.config import get_config
from app.signal_aggregator import (
    AGREEING, CONFLICTING, SPAM_CONFLICT_RATIO_THRESHOLD, SPAM_MIN_SIGNAL_COUNT,
    SOURCE_TRUST_FLOOR, compute_atom_weights, compute_source_trust,
)
from mcp_server.tools.get import get_memory_by_id
from mcp_server.tools.recompute import recompute_memory_atom
from mcp_server.tools.store_auto import store_memory_auto

# ── test artifacts for cleanup ────────────────────────────────────────────────
_test_atom_ids: list[str] = []
_test_signal_ids: list[str] = []


def _cleanup() -> None:
    if not any([_test_signal_ids, _test_atom_ids]):
        print("  No DB artifacts to clean up.")
        return
    config = get_config()
    with psycopg.connect(config.database_url) as conn:
        with conn.cursor() as cur:
            for sid in _test_signal_ids:
                cur.execute("DELETE FROM memory_signals WHERE id = %s", (sid,))
            for aid in _test_atom_ids:
                cur.execute("DELETE FROM memory_atoms WHERE id = %s", (aid,))
        conn.commit()
    print(
        f"  Deleted: {len(_test_signal_ids)} signal(s), {len(_test_atom_ids)} atom(s)."
    )


# ── helpers ───────────────────────────────────────────────────────────────────

def _fresh() -> datetime:
    """A 'just now' timestamp — recency weight ≈ 1.0."""
    return datetime.now(timezone.utc)


def _days_ago(n: float) -> datetime:
    """Timestamp n days in the past."""
    return datetime.now(timezone.utc) - timedelta(days=n)


def _approx(a: float, b: float, tol: float = 1e-4) -> bool:
    return abs(a - b) < tol


def _pass(label: str) -> None:
    print(f"  PASS  {label}")


def _fail(label: str, detail: str) -> None:
    print(f"  FAIL  {label}")
    print(f"        {detail}")
    sys.exit(1)


def _assert(condition: bool, label: str, detail: str = "") -> None:
    if condition:
        _pass(label)
    else:
        _fail(label, detail or "assertion failed")


# ─────────────────────────────────────────────────────────────────────────────
# PART 1 — Unit tests (no DB)
# ─────────────────────────────────────────────────────────────────────────────

def part1_unit_tests() -> None:
    print()
    print("══ Part 1: unit tests (signal_aggregator.compute_atom_weights) ══")

    # 1.1 Empty signal list → all zeros, confidence = 0.5
    w = compute_atom_weights([])
    _assert(w["support_weight"] == 0.0,    "1.1 empty → support_weight = 0.0",    str(w))
    _assert(w["opposition_weight"] == 0.0, "1.1 empty → opposition_weight = 0.0", str(w))
    _assert(w["disagreement_score"] == 0.0,"1.1 empty → disagreement_score = 0.0",str(w))
    _assert(w["confidence"] == 0.5,        "1.1 empty → confidence = 0.5",        str(w))

    # 1.2 Single agreeing signal (relationship="new"), confidence=0.8, fresh
    # Expected: support_weight = 0.8, disagreement = 0.0
    # confidence = 0.5 + 0.5 * (0.8/1.8) = 0.5 + 0.2222 = 0.7222 → rounds to 0.722
    w = compute_atom_weights([
        {"relationship": "new", "confidence": 0.8, "source_key": "u", "created_at": _fresh()},
    ])
    _assert(_approx(w["support_weight"], 0.8, tol=0.01),
            "1.2 single agreeing → support_weight ≈ 0.8", str(w))
    _assert(w["opposition_weight"] == 0.0,
            "1.2 single agreeing → opposition_weight = 0.0", str(w))
    _assert(w["disagreement_score"] == 0.0,
            "1.2 single agreeing → disagreement_score = 0.0", str(w))
    expected_conf = round(0.5 + 0.5 * (0.8 / 1.8), 3)  # 0.722
    _assert(_approx(w["confidence"], expected_conf, tol=0.002),
            f"1.2 single agreeing → confidence ≈ {expected_conf}", str(w))

    # 1.3 Single conflicting signal, confidence=0.8, fresh
    # confidence = 0.5 - 0.5 * (0.8/1.8) ≈ 0.278
    w = compute_atom_weights([
        {"relationship": "conflict", "confidence": 0.8, "source_key": "u", "created_at": _fresh()},
    ])
    _assert(_approx(w["opposition_weight"], 0.8, tol=0.01),
            "1.3 single conflicting → opposition_weight ≈ 0.8", str(w))
    _assert(w["support_weight"] == 0.0,
            "1.3 single conflicting → support_weight = 0.0", str(w))
    _assert(_approx(w["disagreement_score"], 1.0, tol=0.001),
            "1.3 single conflicting → disagreement_score = 1.0", str(w))
    expected_conf = round(0.5 - 0.5 * (0.8 / 1.8), 3)  # 0.278
    _assert(_approx(w["confidence"], expected_conf, tol=0.002),
            f"1.3 single conflicting → confidence ≈ {expected_conf}", str(w))

    # 1.4 Source decay: two "new" signals from the SAME source
    # first:  weight = 0.8 * 1.0 = 0.8
    # second: weight = 0.8 * 0.5 = 0.4   (source decay: n=1 → 0.5^1)
    # total support = 1.2
    t_old = _days_ago(1)  # ensure first comes before second
    t_new = _fresh()
    w_same = compute_atom_weights([
        {"relationship": "new", "confidence": 0.8, "source_key": "alice", "created_at": t_old},
        {"relationship": "new", "confidence": 0.8, "source_key": "alice", "created_at": t_new},
    ])
    _assert(_approx(w_same["support_weight"], 1.2, tol=0.02),
            "1.4 same source × 2 → support_weight ≈ 1.2", str(w_same))

    # 1.5 Two "new" signals from DIFFERENT sources → each gets full decay (n=0)
    # total support = 0.8 + 0.8 = 1.6
    w_diff = compute_atom_weights([
        {"relationship": "new", "confidence": 0.8, "source_key": "alice", "created_at": t_old},
        {"relationship": "new", "confidence": 0.8, "source_key": "bob",   "created_at": t_new},
    ])
    _assert(_approx(w_diff["support_weight"], 1.6, tol=0.02),
            "1.5 different sources × 2 → support_weight ≈ 1.6", str(w_diff))
    _assert(w_diff["support_weight"] > w_same["support_weight"],
            "1.5 different sources outweigh same source (source decay works)", "")

    # 1.6 Recency decay: 60-day-old signal weighs 1/4 vs. fresh signal
    # recency(60d) = exp(-ln(2) * 60/30) = exp(-2*ln2) = 0.25
    expected_old_weight = round(0.8 * 1.0 * math.exp(-math.log(2) * 60 / 30), 4)  # ≈ 0.2
    w_old = compute_atom_weights([
        {"relationship": "new", "confidence": 0.8, "source_key": "u", "created_at": _days_ago(60)},
    ])
    w_fresh = compute_atom_weights([
        {"relationship": "new", "confidence": 0.8, "source_key": "u", "created_at": _fresh()},
    ])
    _assert(_approx(w_old["support_weight"], expected_old_weight, tol=0.01),
            f"1.6 60-day-old signal → support_weight ≈ {expected_old_weight:.4f}", str(w_old))
    _assert(w_fresh["support_weight"] > w_old["support_weight"] * 3,
            "1.6 fresh signal weighs >> 60-day-old signal (recency decay works)",
            f"fresh={w_fresh['support_weight']:.4f}, old={w_old['support_weight']:.4f}")

    # 1.7 Mixed signals: 1 supporting + 1 conflicting (different sources, same freshness)
    # support = 0.8, opposition = 0.8, disagreement = 0.8/1.6 = 0.5
    # confidence = 0.5 + 0.5*(0.8/1.8) - 0.5*(0.8/1.8) = 0.5 (neutral)
    w = compute_atom_weights([
        {"relationship": "new",      "confidence": 0.8, "source_key": "a", "created_at": _fresh()},
        {"relationship": "conflict",  "confidence": 0.8, "source_key": "b", "created_at": _fresh()},
    ])
    _assert(_approx(w["disagreement_score"], 0.5, tol=0.001),
            "1.7 equal support+opposition → disagreement_score = 0.5", str(w))
    _assert(_approx(w["confidence"], 0.5, tol=0.002),
            "1.7 equal support+opposition → confidence = 0.5 (neutral)", str(w))

    # 1.8 Unknown/skipped relationships (duplicate, ask_user, None) are ignored
    w = compute_atom_weights([
        {"relationship": "duplicate",  "confidence": 0.8, "source_key": "u", "created_at": _fresh()},
        {"relationship": "ask_user",   "confidence": 0.8, "source_key": "u", "created_at": _fresh()},
        {"relationship": None,         "confidence": 0.8, "source_key": "u", "created_at": _fresh()},
        {"relationship": "",           "confidence": 0.8, "source_key": "u", "created_at": _fresh()},
    ])
    _assert(w["support_weight"] == 0.0 and w["opposition_weight"] == 0.0,
            "1.8 ignored relationships → all weights = 0.0", str(w))
    _assert(w["confidence"] == 0.5,
            "1.8 ignored relationships → confidence = 0.5 (no evidence)", str(w))

    # 1.9 All AGREEING relationship types are accepted
    for rel in ("new", "reinforcement", "refinement"):
        w = compute_atom_weights([
            {"relationship": rel, "confidence": 0.8, "source_key": "u", "created_at": _fresh()},
        ])
        _assert(w["support_weight"] > 0.0,
                f"1.9 AGREEING relationship '{rel}' → support_weight > 0", str(w))
        _assert(w["opposition_weight"] == 0.0,
                f"1.9 AGREEING relationship '{rel}' → opposition_weight = 0", str(w))

    # 1.10 All CONFLICTING relationship types are accepted
    for rel in ("conflict", "opinion_change"):
        w = compute_atom_weights([
            {"relationship": rel, "confidence": 0.8, "source_key": "u", "created_at": _fresh()},
        ])
        _assert(w["opposition_weight"] > 0.0,
                f"1.10 CONFLICTING relationship '{rel}' → opposition_weight > 0", str(w))
        _assert(w["support_weight"] == 0.0,
                f"1.10 CONFLICTING relationship '{rel}' → support_weight = 0", str(w))

    # 1.11 Confidence clamped to 0.99 with overwhelming support
    # 100 signals from 100 different sources, all fresh agreeing
    many_support = [
        {"relationship": "new", "confidence": 1.0, "source_key": f"u{i}", "created_at": _fresh()}
        for i in range(100)
    ]
    w = compute_atom_weights(many_support)
    _assert(w["confidence"] <= 0.99,
            "1.11 overwhelming support → confidence clamped to ≤ 0.99", str(w))
    _assert(w["confidence"] >= 0.98,
            "1.11 overwhelming support → confidence ≥ 0.98 (near max)", str(w))

    # 1.12 Confidence clamped to 0.1 with overwhelming opposition
    many_conflict = [
        {"relationship": "conflict", "confidence": 1.0, "source_key": f"u{i}", "created_at": _fresh()}
        for i in range(100)
    ]
    w = compute_atom_weights(many_conflict)
    _assert(w["confidence"] >= 0.1,
            "1.12 overwhelming opposition → confidence clamped to ≥ 0.1", str(w))
    _assert(w["confidence"] <= 0.12,
            "1.12 overwhelming opposition → confidence ≤ 0.12 (near min)", str(w))

    # 1.13 Per-type half-life: RECENCY_HALF_LIFE_DAYS_BY_TYPE is importable and has expected keys
    from app.signal_aggregator import RECENCY_HALF_LIFE_DAYS_BY_TYPE
    for expected_type in ("instruction", "decision", "fact", "opinion", "preference"):
        _assert(expected_type in RECENCY_HALF_LIFE_DAYS_BY_TYPE,
                f"1.13 RECENCY_HALF_LIFE_DAYS_BY_TYPE has key '{expected_type}'",
                str(list(RECENCY_HALF_LIFE_DAYS_BY_TYPE.keys())))
    _assert(RECENCY_HALF_LIFE_DAYS_BY_TYPE["instruction"] > RECENCY_HALF_LIFE_DAYS_BY_TYPE["opinion"],
            "1.13 instruction half-life > opinion half-life (instructions are more durable)",
            str(RECENCY_HALF_LIFE_DAYS_BY_TYPE))

    # 1.14 Per-type half-life affects recency decay for a 30-day-old signal.
    # For a confidence=1.0 agreeing signal from 30 days ago:
    #   instruction (180d): weight = exp(-ln2 * 30/180) ≈ 0.891
    #   default     ( 30d): weight = exp(-ln2 * 30/30)  = 0.500
    #   opinion     ( 14d): weight = exp(-ln2 * 30/14)  ≈ 0.226
    sig_30d = [{"relationship": "new", "confidence": 1.0, "source_key": "u", "created_at": _days_ago(30)}]
    w_instruction = compute_atom_weights(sig_30d, memory_type="instruction")
    w_default     = compute_atom_weights(sig_30d)                              # no memory_type
    w_opinion     = compute_atom_weights(sig_30d, memory_type="opinion")

    expected_instruction = math.exp(-math.log(2) * 30 / 180)
    expected_default     = math.exp(-math.log(2) * 30 / 30)
    expected_opinion     = math.exp(-math.log(2) * 30 / 14)

    _assert(_approx(w_instruction["support_weight"], expected_instruction, tol=0.005),
            f"1.14 instruction 30d-old → support_weight ≈ {expected_instruction:.4f}",
            f"got {w_instruction['support_weight']:.4f}")
    _assert(_approx(w_default["support_weight"], expected_default, tol=0.005),
            f"1.14 default 30d-old → support_weight ≈ {expected_default:.4f}",
            f"got {w_default['support_weight']:.4f}")
    _assert(_approx(w_opinion["support_weight"], expected_opinion, tol=0.005),
            f"1.14 opinion 30d-old → support_weight ≈ {expected_opinion:.4f}",
            f"got {w_opinion['support_weight']:.4f}")
    _assert(w_instruction["support_weight"] > w_default["support_weight"] > w_opinion["support_weight"],
            "1.14 ordering: instruction > default > opinion for 30d-old signal", "")

    # 1.15 Unknown / None memory_type falls back to RECENCY_HALF_LIFE_DAYS (30d)
    w_unknown = compute_atom_weights(sig_30d, memory_type="unknown_type")
    w_none    = compute_atom_weights(sig_30d, memory_type=None)
    _assert(_approx(w_unknown["support_weight"], expected_default, tol=0.005),
            "1.15 unknown memory_type → falls back to 30d default", f"got {w_unknown['support_weight']:.4f}")
    _assert(_approx(w_none["support_weight"], expected_default, tol=0.005),
            "1.15 None memory_type → falls back to 30d default", f"got {w_none['support_weight']:.4f}")

    # 1.16 compute_source_trust: importable; returns empty dict for sparse sources
    trust_sparse = compute_source_trust([
        {"source_key": "new_source", "total": 3, "conflicts": 3},  # below MIN
    ])
    _assert(trust_sparse == {},
            "1.16 sparse source (< SPAM_MIN_SIGNAL_COUNT) is not penalised",
            f"got {trust_sparse}")
    trust_low_ratio = compute_source_trust([
        {"source_key": "noisy", "total": 10, "conflicts": 4},  # ratio=0.4, below threshold
    ])
    _assert("noisy" not in trust_low_ratio,
            "1.16 source with conflict ratio < threshold is not penalised",
            f"got {trust_low_ratio}")

    # 1.17 compute_source_trust: adversarial source gets SOURCE_TRUST_FLOOR
    trust_adversarial = compute_source_trust([
        {"source_key": "spammer", "total": 10, "conflicts": 8},   # ratio=0.8 ≥ 0.7
        {"source_key": "trusted", "total": 10, "conflicts": 1},   # ratio=0.1, fine
        {"source_key": "borderline", "total": 10, "conflicts": 7}, # ratio=0.7 exactly = threshold
    ])
    _assert(trust_adversarial.get("spammer") == SOURCE_TRUST_FLOOR,
            f"1.17 adversarial source 'spammer' gets SOURCE_TRUST_FLOOR ({SOURCE_TRUST_FLOOR})",
            f"got {trust_adversarial}")
    _assert("trusted" not in trust_adversarial,
            "1.17 trusted source not penalised", f"got {trust_adversarial}")
    _assert(trust_adversarial.get("borderline") == SOURCE_TRUST_FLOOR,
            f"1.17 borderline source (ratio == threshold) gets SOURCE_TRUST_FLOOR",
            f"got {trust_adversarial}")

    # 1.18 source_trust multiplier reduces adversarial signal weight
    sig_fresh = [{"relationship": "conflict", "confidence": 1.0,
                  "source_key": "spammer", "created_at": _fresh()}]
    w_no_trust  = compute_atom_weights(sig_fresh)
    w_with_trust = compute_atom_weights(sig_fresh, source_trust={"spammer": SOURCE_TRUST_FLOOR})
    _assert(w_with_trust["opposition_weight"] < w_no_trust["opposition_weight"],
            "1.18 adversarial source_trust reduces opposition_weight",
            f"no_trust={w_no_trust['opposition_weight']:.4f}, with_trust={w_with_trust['opposition_weight']:.4f}")
    expected_ratio = SOURCE_TRUST_FLOOR
    actual_ratio = w_with_trust["opposition_weight"] / w_no_trust["opposition_weight"]
    _assert(_approx(actual_ratio, expected_ratio, tol=0.005),
            f"1.18 opposition weight ratio ≈ SOURCE_TRUST_FLOOR ({SOURCE_TRUST_FLOOR})",
            f"actual ratio={actual_ratio:.4f}")
    # None source_trust behaves identically to no-penalisation (no KeyError)
    w_none_trust = compute_atom_weights(sig_fresh, source_trust=None)
    _assert(_approx(w_none_trust["opposition_weight"], w_no_trust["opposition_weight"], tol=1e-9),
            "1.18 source_trust=None behaves as full trust (no change)",
            f"no_trust={w_no_trust['opposition_weight']:.6f}, none_trust={w_none_trust['opposition_weight']:.6f}")


# ─────────────────────────────────────────────────────────────────────────────
# PART 2 — Integration tests (live DB)
# ─────────────────────────────────────────────────────────────────────────────

def part2_integration_tests() -> None:
    print()
    print("══ Part 2: integration tests (live DB) ══")
    config = get_config()
    tag = secrets.token_hex(4)

    # 2.1 store_memory_auto triggers auto-recompute; atom has non-zero aggregation fields
    print()
    print("  --- 2.1 auto-recompute fires on store ---")
    content = f"TEST AGG [{tag}]: phase 4 signal aggregation integration test."
    report = store_memory_auto(
        content=content,
        memory_type="fact",
        relationship="new",
        scope="test",
        confidence=0.8,
    )
    _assert(report["stored"] is True, "2.1 store_memory_auto returns stored=True", str(report))
    atom_id = report["memory_atom_id"]
    signal_id = report["memory_signal_id"]
    _test_atom_ids.append(atom_id)
    _test_signal_ids.append(signal_id)

    # Fetch raw DB values
    with psycopg.connect(config.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT support_weight, opposition_weight, disagreement_score, "
                "confidence, last_recomputed_at FROM memory_atoms WHERE id = %s",
                (atom_id,),
            )
            row = cur.fetchone()

    assert row is not None, f"atom {atom_id} not found in DB"
    db_sw, db_ow, db_ds, db_conf, db_lr = row
    _assert(float(db_sw) > 0.0,
            "2.1 DB: support_weight > 0 after store with relationship=new",
            f"support_weight={db_sw}")
    _assert(float(db_ow) == 0.0,
            "2.1 DB: opposition_weight = 0 (no conflicting signals yet)",
            f"opposition_weight={db_ow}")
    _assert(float(db_ds) == 0.0,
            "2.1 DB: disagreement_score = 0.0", f"disagreement_score={db_ds}")
    _assert(db_lr is not None,
            "2.1 DB: last_recomputed_at is set (not NULL)", str(db_lr))
    _assert(0.5 < float(db_conf) < 0.99,
            "2.1 DB: confidence between 0.5 and 0.99 (supporting evidence)",
            f"confidence={db_conf}")
    print(f"    support_weight={float(db_sw):.4f}  confidence={float(db_conf):.3f}  last_recomputed={str(db_lr)[:19]}")

    # 2.2 get_memory_by_id returns aggregation fields
    print()
    print("  --- 2.2 get_memory_by_id includes aggregation fields ---")
    atom = get_memory_by_id(atom_id)
    assert atom is not None
    for field in ("support_weight", "opposition_weight", "disagreement_score", "last_recomputed_at"):
        _assert(field in atom,
                f"2.2 get_memory_by_id response includes '{field}'", str(list(atom.keys())))
    _assert(atom["support_weight"] > 0.0,
            "2.2 get_memory_by_id: support_weight > 0", str(atom))
    _assert(atom["disagreement_score"] == 0.0,
            "2.2 get_memory_by_id: disagreement_score = 0.0", str(atom))
    print(f"    id={atom['id'][:8]}... confidence={atom['confidence']:.3f}  support={atom['support_weight']:.4f}")

    # 2.3 Adding a conflicting signal raises disagreement_score
    print()
    print("  --- 2.3 conflicting signal raises disagreement_score, drops confidence ---")
    confidence_before = float(db_conf)

    # Insert a conflicting signal directly (simulates an external source reporting a conflict)
    with psycopg.connect(config.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO memory_signals (
                    memory_atom_id, source_key, source_type,
                    content, memory_type, scope,
                    relationship, confidence, importance
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id;
                """,
                (
                    atom_id, "local_user", "local",
                    content, "fact", "test",
                    "conflict", 0.8, 0.5,
                ),
            )
            conflict_signal_id = str(cur.fetchone()[0])
        conn.commit()
    _test_signal_ids.append(conflict_signal_id)
    print(f"    Inserted conflicting signal {conflict_signal_id[:8]}...")

    # Recompute after adding the conflicting signal
    recomputed = recompute_memory_atom(atom_id)
    _assert("error" not in recomputed,
            "2.3 recompute_memory_atom succeeds after adding conflicting signal",
            str(recomputed))

    sw_after  = recomputed["support_weight"]
    ow_after  = recomputed["opposition_weight"]
    ds_after  = recomputed["disagreement_score"]
    conf_after = recomputed["confidence"]

    _assert(ow_after > 0.0,
            "2.3 opposition_weight > 0 after conflicting signal",
            f"opposition_weight={ow_after}")
    _assert(ds_after > 0.0,
            "2.3 disagreement_score > 0 after conflicting signal",
            f"disagreement_score={ds_after}")
    _assert(conf_after < confidence_before,
            "2.3 confidence drops after conflicting signal",
            f"before={confidence_before:.3f}  after={conf_after:.3f}")

    print(f"    Before: confidence={confidence_before:.3f}  disagreement=0.0")
    print(f"    After:  confidence={conf_after:.3f}  disagreement={ds_after:.4f}")
    print(f"            support={sw_after:.4f}  opposition={ow_after:.4f}")

    # 2.4 disagreement_score formula check: ow / (sw + ow)
    expected_ds = ow_after / (sw_after + ow_after)
    _assert(_approx(ds_after, expected_ds, tol=1e-4),
            f"2.4 disagreement_score = opposition/(support+opposition) ≈ {expected_ds:.4f}",
            f"actual={ds_after}")

    # 2.5 Explicit recompute is idempotent (second call returns same values)
    recomputed2 = recompute_memory_atom(atom_id)
    _assert(_approx(recomputed2["support_weight"], sw_after, tol=1e-5),
            "2.5 recompute is idempotent: second call → same support_weight",
            f"{recomputed2['support_weight']} vs {sw_after}")
    _assert(_approx(recomputed2["confidence"], conf_after, tol=1e-3),
            "2.5 recompute is idempotent: second call → same confidence",
            f"{recomputed2['confidence']} vs {conf_after}")

    # 2.6 recompute_memory_atom with unknown ID returns error dict
    not_found = recompute_memory_atom("00000000-0000-0000-0000-000000000000")
    _assert("error" in not_found and not_found["error"] == "not found",
            "2.6 unknown atom ID → {error: not found}", str(not_found))


# ─────────────────────────────────────────────────────────────────────────────
# PART 3 — CLI smoke test
# ─────────────────────────────────────────────────────────────────────────────

def part3_cli_test() -> None:
    print()
    print("══ Part 3: CLI smoke test (make recompute-weights) ══")
    print()

    result = subprocess.run(
        ["make", "recompute-weights"],
        cwd=str(Path(__file__).resolve().parent.parent),
        capture_output=True,
        text=True,
    )
    output = (result.stdout + result.stderr).strip()
    # Print a sample of the output
    lines = output.splitlines()
    for line in lines[-8:]:
        print(f"  {line}")

    _assert(result.returncode == 0,
            "3.1 make recompute-weights exits 0",
            f"exit code={result.returncode}\n{output[-300:]}")
    _assert("Done." in output or "atom(s)" in output,
            "3.1 output contains 'Done.' or 'atom(s)'",
            output[-200:])


# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    try:
        part1_unit_tests()
        part2_integration_tests()
        part3_cli_test()
        print()
        print("══ All Phase 4 aggregation tests passed. ══")
    finally:
        print()
        print("=== cleanup ===")
        _cleanup()


if __name__ == "__main__":
    main()
