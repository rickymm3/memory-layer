"""Local dashboard for the memory-layer system.

Run via:
    make dashboard
    # then open http://localhost:5001

All read-only routes use direct psycopg queries with parameterised SQL.
The /chat route writes through the same write policy as the MCP tools:
  - new/refinement candidates → auto-stored (memory_atom + memory_signal)
  - conflict/opinion_change   → queued to memory_proposals for CLI review
  - duplicate/reinforcement   → skipped (no write)
"""
from __future__ import annotations

import json
import os
import uuid

from flask import Flask, abort, redirect, render_template, request, session, url_for

import psycopg

from app.chat import (
    chat_with_history,
    chat_with_research,
    clean_assistant_response,
)
from app.config import get_config
from app.db import get_store
from app.generation import get_image_provider, get_video_provider, list_recent_images

app = Flask(__name__)
app.secret_key = os.environ.get("DASHBOARD_SECRET_KEY", "memory-layer-local-dev-only")
app.config["TEMPLATES_AUTO_RELOAD"] = True

_HISTORY_LIMIT = 100  # max messages stored per DB session (50 turns)


# ── DB-backed session helpers ─────────────────────────────────────────────────
# The Flask cookie holds only a UUID session_id (< 40 bytes).
# Full chat history (potentially many KB) lives in dashboard_sessions JSONB.

def _session_load(session_id: str | None) -> list[dict]:
    """Load chat history from DB for the given session_id. Returns [] on miss."""
    if not session_id:
        return []
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT history FROM dashboard_sessions WHERE id = %s;",
                    (session_id,),
                )
                row = cur.fetchone()
        return list(row[0]) if row else []
    except Exception:
        return []


def _session_save(session_id: str | None, history: list[dict]) -> str:
    """Upsert chat history to DB. Returns the session_id (new UUID if none given)."""
    sid = session_id or str(uuid.uuid4())
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO dashboard_sessions (id, history, updated_at)
                    VALUES (%s, %s::jsonb, NOW())
                    ON CONFLICT (id) DO UPDATE
                        SET history = EXCLUDED.history,
                            updated_at = NOW();
                    """,
                    (sid, json.dumps(history)),
                )
                # Prune sessions inactive for more than 30 days
                cur.execute(
                    "DELETE FROM dashboard_sessions WHERE updated_at < NOW() - INTERVAL '30 days';"
                )
            conn.commit()
    except Exception:
        pass
    return sid


# ── DB helpers ────────────────────────────────────────────────────────────────

def _conn():
    return psycopg.connect(get_config().database_url)


def _scopes() -> list[str]:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT scope FROM memory_atoms WHERE scope IS NOT NULL ORDER BY scope;"
            )
            return [r[0] for r in cur.fetchall()]


def _types() -> list[str]:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT memory_type FROM memory_atoms WHERE memory_type IS NOT NULL ORDER BY memory_type;"
            )
            return [r[0] for r in cur.fetchall()]


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def atoms():
    scope_filter = request.args.get("scope", "").strip() or None
    type_filter = request.args.get("type", "").strip() or None
    try:
        min_conf = float(request.args.get("min_confidence", 0.0))
    except ValueError:
        min_conf = 0.0
    min_conf = max(0.0, min(1.0, min_conf))

    params: list = [min_conf]
    where = ["confidence >= %s"]
    if scope_filter:
        where.append("scope = %s")
        params.append(scope_filter)
    if type_filter:
        where.append("memory_type = %s")
        params.append(type_filter)

    where_sql = " AND ".join(where)
    params.append(100)

    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT id, content, memory_type, scope, confidence, importance,
                       support_weight, opposition_weight, disagreement_score,
                       disagreement_score >= 0.5 AS disagreement_flag,
                       last_recomputed_at, created_at,
                       lifecycle_status, retrieval_priority,
                       source_type, source_url
                FROM memory_atoms
                WHERE {where_sql}
                ORDER BY created_at DESC
                LIMIT %s;
                """,
                params,
            )
            rows = cur.fetchall()

    atoms_list = [
        {
            "id": str(r[0]),
            "content": r[1],
            "memory_type": r[2],
            "scope": r[3],
            "confidence": round(float(r[4]), 3),
            "importance": round(float(r[5]), 3),
            "support_weight": round(float(r[6]), 4),
            "opposition_weight": round(float(r[7]), 4),
            "disagreement_score": round(float(r[8]), 4),
            "disagreement_flag": bool(r[9]),
            "last_recomputed_at": r[10].strftime("%Y-%m-%d %H:%M") if r[10] else "—",
            "created_at": r[11].strftime("%Y-%m-%d %H:%M") if r[11] else "—",
            "lifecycle_status": r[12],
            "retrieval_priority": round(float(r[13]), 3),
            "source_type": r[14] or "local",
            "source_url": r[15],
        }
        for r in rows
    ]

    return render_template(
        "atoms.html",
        atoms=atoms_list,
        scopes=_scopes(),
        types=_types(),
        scope_filter=scope_filter or "",
        type_filter=type_filter or "",
        min_conf=min_conf,
    )


@app.route("/atom/<uuid:atom_id>")
def atom_detail(atom_id):
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, content, context_summary, memory_type, scope,
                       confidence, importance, support_weight, opposition_weight,
                       disagreement_score, disagreement_score >= 0.5,
                       last_recomputed_at, created_at,
                       lifecycle_status, superseded_by_atom_id,
                       lifecycle_reason, retrieval_priority, lifecycle_updated_at,
                       source_type, source_url
                FROM memory_atoms WHERE id = %s;
                """,
                (str(atom_id),),
            )
            row = cur.fetchone()
        if row is None:
            abort(404)

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, source_key, source_type, relationship, confidence,
                       reconciliation_reason, created_at
                FROM memory_signals
                WHERE memory_atom_id = %s
                ORDER BY created_at DESC;
                """,
                (str(atom_id),),
            )
            sig_rows = cur.fetchall()

    atom = {
        "id": str(row[0]),
        "content": row[1],
        "context_summary": row[2],
        "memory_type": row[3],
        "scope": row[4],
        "confidence": round(float(row[5]), 3),
        "importance": round(float(row[6]), 3),
        "support_weight": round(float(row[7]), 4),
        "opposition_weight": round(float(row[8]), 4),
        "disagreement_score": round(float(row[9]), 4),
        "disagreement_flag": bool(row[10]),
        "last_recomputed_at": row[11].strftime("%Y-%m-%d %H:%M:%S") if row[11] else "—",
        "created_at": row[12].strftime("%Y-%m-%d %H:%M:%S") if row[12] else "—",
        "lifecycle_status": row[13],
        "superseded_by_atom_id": str(row[14]) if row[14] else None,
        "lifecycle_reason": row[15],
        "retrieval_priority": round(float(row[16]), 3),
        "lifecycle_updated_at": row[17].strftime("%Y-%m-%d %H:%M:%S") if row[17] else None,
        "source_type": row[18] or "local",
        "source_url": row[19],
    }

    signals = [
        {
            "id": str(s[0]),
            "source_key": s[1],
            "source_type": s[2],
            "relationship": s[3],
            "confidence": round(float(s[4]), 3),
            "reconciliation_reason": s[5] or "—",
            "created_at": s[6].strftime("%Y-%m-%d %H:%M:%S") if s[6] else "—",
        }
        for s in sig_rows
    ]

    return render_template("atom_detail.html", atom=atom, signals=signals)


@app.route("/contested")
def contested():
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, content, memory_type, scope, confidence,
                       support_weight, opposition_weight, disagreement_score,
                       created_at
                FROM memory_atoms
                WHERE disagreement_score >= 0.5
                ORDER BY disagreement_score DESC;
                """
            )
            rows = cur.fetchall()

    atoms_list = [
        {
            "id": str(r[0]),
            "content": r[1],
            "memory_type": r[2],
            "scope": r[3],
            "confidence": round(float(r[4]), 3),
            "support_weight": round(float(r[5]), 4),
            "opposition_weight": round(float(r[6]), 4),
            "disagreement_score": round(float(r[7]), 4),
            "created_at": r[8].strftime("%Y-%m-%d %H:%M") if r[8] else "—",
        }
        for r in rows
    ]

    return render_template("contested.html", atoms=atoms_list)


@app.route("/proposals")
def proposals():
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, content, memory_type, scope, relationship,
                       confidence, importance, status, created_at
                FROM memory_proposals
                WHERE status = 'pending_review'
                ORDER BY created_at DESC;
                """
            )
            rows = cur.fetchall()

    proposals_list = [
        {
            "id": str(r[0]),
            "content": r[1],
            "memory_type": r[2],
            "scope": r[3] or "—",
            "relationship": r[4],
            "confidence": round(float(r[5]), 3),
            "importance": round(float(r[6]), 3),
            "status": r[7],
            "created_at": r[8].strftime("%Y-%m-%d %H:%M") if r[8] else "—",
        }
        for r in rows
    ]

    return render_template("proposals.html", proposals=proposals_list)


@app.route("/scopes")
def scopes():
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    COALESCE(scope, '(none)') AS scope_display,
                    scope                     AS scope_raw,
                    COUNT(*)                  AS total,
                    COUNT(*) FILTER (WHERE disagreement_score >= 0.5) AS contested,
                    COUNT(*) FILTER (WHERE confidence < 0.5)          AS low_conf,
                    MAX(created_at)           AS newest,
                    ROUND(AVG(confidence)::numeric, 3)                AS avg_conf
                FROM memory_atoms
                GROUP BY scope
                ORDER BY total DESC;
                """
            )
            rows = cur.fetchall()

    scopes_list = [
        {
            "scope_display": r[0],
            "scope_raw": r[1],
            "total": r[2],
            "contested": r[3],
            "low_conf": r[4],
            "newest": r[5].strftime("%Y-%m-%d %H:%M") if r[5] else "\u2014",
            "avg_conf": round(float(r[6]), 3) if r[6] is not None else None,
        }
        for r in rows
    ]

    return render_template("scopes.html", scopes=scopes_list)


@app.route("/chat", methods=["GET", "POST"])
def chat():
    session_id: str | None = session.get("chat_session_id")
    history: list[dict] = _session_load(session_id)
    error = None

    if request.method == "POST":
        message = request.form.get("message", "").strip()
        if message:
            try:
                messages_for_llm = [
                    {"role": m["role"], "content": m["content"]}
                    for m in history
                ] + [{"role": "user", "content": message}]

                raw_answer, memories, research_results, research_status, context_eval, gap_state, route = chat_with_research(
                    messages_for_llm
                )
                answer = clean_assistant_response(raw_answer)

                history.append({"role": "user", "content": message})

                history.append({
                    "role": "assistant",
                    "content": answer,
                    "memories": [
                        {
                            "id": str(m.get("id", "")),
                            "content": (m.get("context_summary") or m.get("content", ""))[:200],
                            "memory_type": m.get("memory_type", ""),
                            "scope": m.get("scope") or "—",
                            "similarity": round(float(m.get("similarity", 0.0)), 3),
                        }
                        for m in memories
                    ],
                    "write_events": [],
                    "research_status": research_status,
                    "research_count": len(research_results),
                    "gap_info": {
                        "status": gap_state.status,
                        "searches": len(gap_state.searched_queries),
                        "clarifying_question": gap_state.clarifying_question,
                    } if gap_state else None,
                    "route": route,
                    "context_eval": {
                        "context_status": context_eval.context_status,
                        "confidence": round(context_eval.confidence, 2),
                        "final_action": context_eval.final_action,
                        "used_count": len(context_eval.used_atom_ids),
                        "ignored_count": len(context_eval.ignored_atom_ids),
                        "issues_count": len(context_eval.issues),
                        "trace_id": context_eval.trace_id,
                    } if context_eval else None,
                })

                # Trim history length then persist to DB
                if len(history) > _HISTORY_LIMIT:
                    history = history[-_HISTORY_LIMIT:]

                session_id = _session_save(session_id, history)
                session["chat_session_id"] = session_id  # tiny UUID in cookie
            except Exception as exc:
                error = str(exc)
        else:
            error = "Please enter a message."

    return render_template("chat.html", history=history, error=error)


@app.route("/chat/clear", methods=["POST"])
def chat_clear():
    session_id = session.pop("chat_session_id", None)
    if session_id:
        try:
            with _conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM dashboard_sessions WHERE id = %s;", (session_id,))
                conn.commit()
        except Exception:
            pass
    return redirect(url_for("chat"))


@app.errorhandler(404)
def not_found(e):
    return render_template("404.html"), 404


# Category mapping (mirrors model_report.py CLI)
_MODEL_CATEGORY = {
    "instruction": "Prompt Adaptations",
    "fact": "Model Observations",
    "decision": "Model Observations",
}
_CATEGORY_ORDER = ["Model Observations", "Prompt Adaptations", "General"]


@app.route("/model_lessons")
def model_lessons():
    model_filter = request.args.get("model", "").strip() or None
    include_inactive = request.args.get("include_inactive", "").strip() == "1"

    if model_filter:
        scope_filter = f"model:{model_filter}"
        scope_clause = "AND scope = %s"
        scope_params: list = [scope_filter]
    else:
        scope_clause = "AND scope LIKE %s"
        scope_params = ["model:%"]

    lifecycle_clause = "" if include_inactive else "AND lifecycle_status = 'active'"

    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT id, scope, content, memory_type,
                       confidence, importance, lifecycle_status, created_at
                FROM memory_atoms
                WHERE 1=1
                  {scope_clause}
                  {lifecycle_clause}
                ORDER BY scope, importance DESC NULLS LAST, confidence DESC NULLS LAST;
                """,
                scope_params,
            )
            rows = cur.fetchall()

    # Discover available model names (model:* scopes, active only)
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT scope FROM memory_atoms WHERE scope LIKE 'model:%' ORDER BY scope;"
            )
            model_scopes = [r[0].removeprefix("model:") for r in cur.fetchall()]

    # Group by scope → category
    by_scope: dict[str, dict[str, list]] = {}
    for atom_id, scope, content, mtype, conf, imp, status, created_at in rows:
        by_scope.setdefault(scope, {cat: [] for cat in _CATEGORY_ORDER})
        cat = _MODEL_CATEGORY.get(mtype or "", "General")
        by_scope[scope][cat].append({
            "id": str(atom_id),
            "id_short": str(atom_id)[:8],
            "content": content,
            "memory_type": mtype,
            "confidence": round(float(conf), 3) if conf is not None else None,
            "importance": round(float(imp), 3) if imp is not None else None,
            "lifecycle_status": status,
            "created_at": created_at.strftime("%Y-%m-%d") if created_at else "—",
        })

    total = sum(
        sum(len(v) for v in cats.values()) for cats in by_scope.values()
    )

    return render_template(
        "model_lessons.html",
        by_scope=by_scope,
        category_order=_CATEGORY_ORDER,
        model_scopes=model_scopes,
        model_filter=model_filter or "",
        include_inactive=include_inactive,
        total=total,
    )


@app.route("/task_runs")
def task_runs():
    scope_filter = request.args.get("scope", "").strip() or None
    outcome_filter = request.args.get("outcome", "").strip() or None

    conditions: list[str] = []
    params: list = []
    if scope_filter:
        conditions.append("scope = %s")
        params.append(scope_filter)
    if outcome_filter:
        conditions.append("outcome = %s")
        params.append(outcome_filter)
    where_sql = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params.append(100)

    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT id, scope, task_description, model_used,
                       outcome, lessons_stored, created_at
                FROM task_runs
                {where_sql}
                ORDER BY created_at DESC
                LIMIT %s;
                """,
                params,
            )
            rows = cur.fetchall()

    runs = [
        {
            "id": str(r[0]),
            "id_short": str(r[0])[:8],
            "scope": r[1],
            "task_description": r[2],
            "model_used": r[3] or "—",
            "outcome": r[4],
            "lessons_stored": r[5],
            "created_at": r[6].strftime("%Y-%m-%d %H:%M") if r[6] else "—",
        }
        for r in rows
    ]

    return render_template(
        "task_runs.html",
        runs=runs,
        scopes=_scopes(),
        scope_filter=scope_filter or "",
        outcome_filter=outcome_filter or "",
    )


@app.route("/commits")
def commits():
    from app.memory_store import MemoryStore
    decision_filter = request.args.get("decision", "").strip() or None
    traces = MemoryStore().list_commit_traces(limit=100, decision_filter=decision_filter)
    return render_template(
        "commits.html",
        traces=traces,
        decision_filter=decision_filter or "",
    )


@app.route("/context_traces")
def context_traces():
    from app.memory_store import MemoryStore
    status_filter = request.args.get("status", "").strip() or None
    action_filter = request.args.get("action", "").strip() or None
    traces = MemoryStore().list_context_traces(
        limit=100,
        status_filter=status_filter,
        action_filter=action_filter,
    )
    return render_template(
        "context_traces.html",
        traces=traces,
        status_filter=status_filter or "",
        action_filter=action_filter or "",
    )


@app.route("/response_traces")
def response_traces():
    from app.memory_store import MemoryStore
    verdict_filter = request.args.get("verdict", "").strip() or None
    traces = MemoryStore().list_response_traces(limit=100, verdict_filter=verdict_filter)
    return render_template(
        "response_traces.html",
        traces=traces,
        verdict_filter=verdict_filter or "",
    )


@app.route("/beliefs")
def beliefs():
    scope_filter = request.args.get("scope", "").strip() or None
    type_filter = request.args.get("type", "").strip() or None
    revisable_types = ("opinion", "preference", "belief", "instruction", "decision", "correction", "warning")

    type_clause = "memory_type = %s" if type_filter else "memory_type = ANY(%s)"
    params: list = [type_filter if type_filter else list(revisable_types)]
    if scope_filter:
        params.append(scope_filter)
    params.append(100)

    scope_sql = "AND scope = %s" if scope_filter else ""

    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT a.id, a.content, a.memory_type, a.scope,
                       a.confidence, a.support_weight, a.opposition_weight,
                       a.disagreement_score, a.lifecycle_status, a.created_at,
                       COUNT(brl.id) AS revision_count
                FROM memory_atoms a
                LEFT JOIN belief_revision_log brl ON brl.atom_id = a.id
                WHERE {type_clause} {scope_sql}
                GROUP BY a.id
                ORDER BY revision_count DESC, a.created_at DESC
                LIMIT %s;
                """,
                params,
            )
            rows = cur.fetchall()

    atoms_list = [
        {
            "id": str(r[0]),
            "content": r[1],
            "memory_type": r[2],
            "scope": r[3] or "—",
            "confidence": round(float(r[4]), 3),
            "support_weight": round(float(r[5]), 4),
            "opposition_weight": round(float(r[6]), 4),
            "disagreement_score": round(float(r[7]), 4),
            "lifecycle_status": r[8] or "active",
            "created_at": r[9].strftime("%Y-%m-%d") if r[9] else "—",
            "revision_count": int(r[10]),
        }
        for r in rows
    ]

    return render_template(
        "beliefs.html",
        atoms=atoms_list,
        scopes=_scopes(),
        revisable_types=list(revisable_types),
        scope_filter=scope_filter or "",
        type_filter=type_filter or "",
    )

@app.route("/prompt_history")
def prompt_history():
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    rt.id,
                    rt.user_message,
                    rt.verdict,
                    rt.created_at,
                    ct.context_status,
                    ct.final_action,
                    ct.confidence,
                    COALESCE(jsonb_array_length(ct.used_atom_ids), 0)       AS used_count,
                    COALESCE(jsonb_array_length(ct.retrieved_atom_ids), 0)  AS retrieved_count
                FROM runtime_response_traces rt
                LEFT JOIN runtime_context_traces ct ON ct.id = rt.context_trace_id
                ORDER BY rt.created_at DESC
                LIMIT 200;
                """
            )
            rows = cur.fetchall()

    turns = [
        {
            "id": str(r[0]),
            "user_message": r[1] or "",
            "verdict": r[2],
            "created_at": r[3].strftime("%Y-%m-%d %H:%M:%S") if r[3] else "—",
            "context_status": r[4] or "—",
            "final_action": r[5] or "—",
            "confidence": round(float(r[6]), 2) if r[6] is not None else None,
            "used_count": int(r[7]),
            "retrieved_count": int(r[8]),
        }
        for r in rows
    ]

    return render_template("prompt_history.html", turns=turns)


@app.route("/prompt_history/<uuid:trace_id>")
def prompt_history_detail(trace_id):
    with _conn() as conn:
        with conn.cursor() as cur:
            # Main response trace
            cur.execute(
                """
                SELECT rt.id, rt.user_message, rt.draft_answer, rt.final_answer,
                       rt.verdict, rt.overstatement_risk, rt.issues,
                       rt.commit_candidates, rt.reasoning, rt.created_at,
                       rt.context_trace_id,
                       ct.context_status, ct.final_action, ct.confidence,
                       ct.retrieved_atom_ids, ct.used_atom_ids, ct.ignored_atom_ids,
                       ct.issues AS ctx_issues, ct.required_actions, ct.task_summary
                FROM runtime_response_traces rt
                LEFT JOIN runtime_context_traces ct ON ct.id = rt.context_trace_id
                WHERE rt.id = %s;
                """,
                (str(trace_id),),
            )
            row = cur.fetchone()
        if row is None:
            abort(404)

        # Collect all atom IDs referenced
        retrieved_ids = [str(x) for x in (row[14] or [])]
        used_ids      = set(str(x) for x in (row[15] or []))
        ignored_ids   = set(str(x) for x in (row[16] or []))
        all_ids = list(set(retrieved_ids) | used_ids | ignored_ids)

        atoms_by_id: dict = {}
        if all_ids:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, content, context_summary, memory_type, scope,
                           confidence, similarity
                    FROM memory_atoms
                    CROSS JOIN LATERAL (SELECT 0.0::float AS similarity) s
                    WHERE id = ANY(%s::uuid[]);
                    """,
                    (all_ids,),
                )
                for ar in cur.fetchall():
                    atoms_by_id[str(ar[0])] = {
                        "id": str(ar[0]),
                        "content": ar[1],
                        "context_summary": ar[2],
                        "memory_type": ar[3],
                        "scope": ar[4] or "—",
                        "confidence": round(float(ar[5]), 3) if ar[5] is not None else None,
                    }

    turn = {
        "id": str(row[0]),
        "user_message": row[1] or "",
        "draft_answer": row[2] or "",
        "final_answer": row[3] or "",
        "verdict": row[4],
        "overstatement_risk": row[5] or "none",
        "issues": row[6] or [],
        "commit_candidates": row[7] or [],
        "reasoning": row[8] or "",
        "created_at": row[9].strftime("%Y-%m-%d %H:%M:%S") if row[9] else "—",
        "context_trace_id": str(row[10]) if row[10] else None,
        "context_status": row[11] or "—",
        "final_action": row[12] or "—",
        "ctx_confidence": round(float(row[13]), 2) if row[13] is not None else None,
        "ctx_issues": row[17] or [],
        "required_actions": row[18] or [],
        "task_summary": row[19] or "",
        "used_atoms":     [atoms_by_id[i] for i in retrieved_ids if i in used_ids and i in atoms_by_id],
        "ignored_atoms":  [atoms_by_id[i] for i in retrieved_ids if i in ignored_ids and i in atoms_by_id],
        "other_atoms":    [atoms_by_id[i] for i in retrieved_ids
                           if i not in used_ids and i not in ignored_ids and i in atoms_by_id],
    }

    return render_template("prompt_history_detail.html", turn=turn)


@app.route("/task_run/<uuid:run_id>")
def task_run_detail(run_id):
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, scope, task_description, model_used, files_changed,
                       test_results, outcome, notes, lessons_stored, created_at
                FROM task_runs WHERE id = %s;
                """,
                (str(run_id),),
            )
            row = cur.fetchone()
        if row is None:
            abort(404)

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT ma.id, ma.content, ma.context_summary,
                       ma.memory_type, ma.scope, ma.confidence, ma.lifecycle_status
                FROM memory_signals ms
                JOIN memory_atoms ma ON ma.id = ms.memory_atom_id
                WHERE ms.task_run_id = %s
                ORDER BY ma.id;
                """,
                (str(run_id),),
            )
            lesson_rows = cur.fetchall()

    run = {
        "id": str(row[0]),
        "scope": row[1],
        "task_description": row[2],
        "model_used": row[3] or "—",
        "files_changed": row[4] or "—",
        "test_results": row[5] or "—",
        "outcome": row[6],
        "notes": row[7] or "—",
        "lessons_stored": row[8],
        "created_at": row[9].strftime("%Y-%m-%d %H:%M:%S") if row[9] else "—",
    }

    lessons = [
        {
            "id": str(r[0]),
            "content": r[1],
            "context_summary": r[2],
            "memory_type": r[3],
            "scope": r[4],
            "confidence": round(float(r[5]), 3),
            "lifecycle_status": r[6],
        }
        for r in lesson_rows
    ]

    return render_template("task_run_detail.html", run=run, lessons=lessons)


# ── Generation ────────────────────────────────────────────────────────────────

@app.route("/health")
def health():
    report = get_store().health_report()
    return render_template("health.html", r=report)


@app.route("/context-stats")
def context_stats():
    try:
        report = get_store().context_efficiency()
    except Exception as e:
        report = {
            "error": str(e),
            "avg_retrieval_efficiency": None,
            "fat_atoms": [],
            "daily_token_trend": [],
        }
    return render_template("context_stats.html", r=report)


@app.route("/generate")
def generate():
    img_provider = get_image_provider()
    vid_provider = get_video_provider()
    last_image = session.pop("last_image", None)
    last_video = session.pop("last_video", None)
    recent = list_recent_images(24)
    return render_template(
        "generate.html",
        image_provider=img_provider.name,
        image_available=img_provider.available,
        video_provider=vid_provider.name,
        video_available=vid_provider.available,
        last_image=last_image,
        last_video=last_video,
        gallery=recent,
    )


@app.route("/generate/image", methods=["POST"])
def generate_image():
    prompt = request.form.get("prompt", "").strip()
    size = request.form.get("size", "1024x1024")
    result = get_image_provider().generate(prompt=prompt, size=size)
    session["last_image"] = result.to_dict()
    return redirect(url_for("generate"))


@app.route("/generate/video", methods=["POST"])
def generate_video():
    prompt = request.form.get("prompt", "").strip()
    result = get_video_provider().generate(prompt=prompt)
    session["last_video"] = result.to_dict()
    return redirect(url_for("generate"))

