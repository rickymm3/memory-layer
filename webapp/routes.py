"""Public site Blueprint — landing, auth, user brain dashboard, settings."""
from __future__ import annotations

import json
import uuid

import psycopg
from flask import (
    Blueprint,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_login import current_user, login_required, login_user, logout_user

from app.config import get_config
from webapp.auth import User

site_bp = Blueprint("webapp", __name__, template_folder="templates")


def _conn():
    return psycopg.connect(get_config().database_url)


def _ago(dt) -> str:
    if not dt:
        return "—"
    from datetime import timezone
    now = __import__("datetime").datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    diff = int((now - dt).total_seconds())
    if diff < 60: return "just now"
    if diff < 3600: return f"{diff // 60}m ago"
    if diff < 86400: return f"{diff // 3600}h ago"
    return f"{diff // 86400}d ago"


# ── Public routes ─────────────────────────────────────────────────────────────

@site_bp.route("/")
def landing():
    recent = []
    novel = []
    new_since_visit: list[dict] = []
    last_seen_at = None
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                # For logged-in users: surface discussions with new activity since last visit
                if current_user.is_authenticated:
                    cur.execute(
                        "SELECT last_seen_at FROM users WHERE username = %s;",
                        (current_user.username,),
                    )
                    row = cur.fetchone()
                    last_seen_at = row[0] if row else None
                    if last_seen_at:
                        cur.execute(
                            """
                            SELECT id, title, topic_tags, contributor_count,
                                   atom_count, novelty_flag, last_activity_at, thread_status
                            FROM discussions
                            WHERE last_activity_at > %s
                            ORDER BY last_activity_at DESC
                            LIMIT 10;
                            """,
                            (last_seen_at,),
                        )
                        for r in cur.fetchall():
                            new_since_visit.append({
                                "id": str(r[0]), "title": r[1], "topic_tags": r[2] or [],
                                "contributor_count": r[3], "atom_count": r[4],
                                "novelty_flag": r[5],
                                "last_activity": _ago(r[6]),
                                "thread_status": r[7] or "active",
                            })
                    # Update last_seen_at on every home page visit
                    cur.execute(
                        "UPDATE users SET last_seen_at = now() WHERE username = %s;",
                        (current_user.username,),
                    )
                    conn.commit()
                cur.execute(
                    """
                    SELECT id, title, topic_tags, contributor_count, atom_count,
                           novelty_flag, last_activity_at, thread_status
                    FROM discussions
                    ORDER BY last_activity_at DESC
                    LIMIT 20;
                    """
                )
                for r in cur.fetchall():
                    recent.append({
                        "id": str(r[0]), "title": r[1], "topic_tags": r[2] or [],
                        "contributor_count": r[3], "atom_count": r[4],
                        "novelty_flag": r[5],
                        "last_activity": _ago(r[6]),
                        "thread_status": r[7] or "active",
                    })
                cur.execute(
                    """
                    SELECT id, title, topic_tags, contributor_count, atom_count, last_activity_at
                    FROM discussions WHERE novelty_flag = true
                    ORDER BY last_activity_at DESC LIMIT 5;
                    """
                )
                for r in cur.fetchall():
                    novel.append({
                        "id": str(r[0]), "title": r[1], "topic_tags": r[2] or [],
                        "contributor_count": r[3], "atom_count": r[4],
                        "last_activity": _ago(r[5]),
                    })
    except Exception:
        pass
    return render_template("site/landing.html", recent=recent, novel=novel,
                           new_since_visit=new_since_visit, last_seen_at=last_seen_at)


@site_bp.route("/signup", methods=["GET", "POST"])
def signup():
    if current_user.is_authenticated:
        return redirect(url_for("webapp.brain"))
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip() or None
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")
        if not username or not password:
            error = "Username and password are required."
        elif len(password) < 8:
            error = "Password must be at least 8 characters."
        elif password != confirm:
            error = "Passwords do not match."
        elif User.get_by_username(username):
            error = "That username is already taken."
        else:
            user = User.create(username=username, password=password, email=email)
            if user:
                login_user(user)
                return redirect(url_for("webapp.brain"))
            error = "Account creation failed. Please try again."
    return render_template("site/signup.html", error=error)


@site_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("webapp.brain"))
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = User.get_by_username(username)
        if user and user.is_active and user.check_password(password):
            login_user(user)
            next_url = request.args.get("next")
            return redirect(next_url or url_for("webapp.brain"))
        error = "Invalid username or password."
    return render_template("site/login.html", error=error)


@site_bp.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    return redirect(url_for("webapp.landing"))


# ── Authenticated routes ──────────────────────────────────────────────────────

@site_bp.route("/brain")
@login_required
def brain():
    atoms = []
    stats = {"total": 0, "facts": 0, "decisions": 0, "contested": 0}
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT DISTINCT ma.id, ma.content, ma.memory_type, ma.scope,
                           ma.confidence, ma.importance, ma.lifecycle_status, ma.created_at,
                           ma.disagreement_score, ma.visibility
                    FROM memory_atoms ma
                    JOIN memory_signals ms ON ms.memory_atom_id = ma.id
                    WHERE ms.source_user_id = %s
                      AND ma.lifecycle_status = 'active'
                    ORDER BY ma.importance DESC, ma.created_at DESC
                    LIMIT 50;
                    """,
                    (current_user.username,),
                )
                rows = cur.fetchall()
                atoms = [
                    {
                        "id": str(r[0]),
                        "content": r[1],
                        "memory_type": r[2],
                        "scope": r[3] or "—",
                        "confidence": round(float(r[4]), 2),
                        "importance": round(float(r[5]), 2),
                        "lifecycle_status": r[6],
                        "created_at": r[7].strftime("%Y-%m-%d") if r[7] else "—",
                        "contested": float(r[8] or 0) >= 0.5,
                        "visibility": r[9] or "private",
                    }
                    for r in rows
                ]
                # Stats
                cur.execute(
                    """
                    SELECT
                        COUNT(DISTINCT ma.id),
                        COUNT(DISTINCT ma.id) FILTER (WHERE ma.memory_type = 'fact'),
                        COUNT(DISTINCT ma.id) FILTER (WHERE ma.memory_type = 'decision'),
                        COUNT(DISTINCT ma.id) FILTER (WHERE ma.disagreement_score >= 0.5)
                    FROM memory_atoms ma
                    JOIN memory_signals ms ON ms.memory_atom_id = ma.id
                    WHERE ms.source_user_id = %s AND ma.lifecycle_status = 'active';
                    """,
                    (current_user.username,),
                )
                sr = cur.fetchone()
                if sr:
                    stats = {
                        "total": sr[0] or 0,
                        "facts": sr[1] or 0,
                        "decisions": sr[2] or 0,
                        "contested": sr[3] or 0,
                    }
    except Exception:
        pass
    return render_template("site/brain.html", atoms=atoms, stats=stats)


@site_bp.route("/settings")
@login_required
def settings():
    return render_template("site/settings.html")


@site_bp.route("/settings/rotate-token", methods=["POST"])
@login_required
def rotate_token():
    current_user.rotate_token()
    flash("API token rotated. Update your MCP configs with the new token.", "success")
    return redirect(url_for("webapp.settings"))


# ── Atom visibility toggle ────────────────────────────────────────────────────

@site_bp.route("/atom/<uuid:atom_id>/visibility", methods=["POST"])
@login_required
def toggle_visibility(atom_id):
    new_vis = request.form.get("visibility", "private")
    if new_vis not in ("private", "public"):
        new_vis = "private"
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                # Only allow toggling atoms that belong to this user
                cur.execute(
                    """
                    UPDATE memory_atoms ma SET visibility = %s
                    FROM memory_signals ms
                    WHERE ms.memory_atom_id = ma.id
                      AND ms.source_user_id = %s
                      AND ma.id = %s;
                    """,
                    (new_vis, current_user.username, str(atom_id)),
                )
            conn.commit()
    except Exception:
        pass
    return redirect(request.referrer or url_for("webapp.brain"))


# ── Web capture (ingest without MCP) ─────────────────────────────────────────

@site_bp.route("/capture", methods=["GET", "POST"])
@login_required
def capture():
    if request.method == "GET":
        return render_template("site/capture.html")

    content = request.form.get("content", "").strip()
    memory_type = request.form.get("memory_type", "observation").strip()
    make_private = request.form.get("make_private") == "1"
    visibility = "private" if make_private else "public"

    if not content:
        flash("Content cannot be empty.", "error")
        return render_template("site/capture.html")

    try:
        from dotenv import load_dotenv
        load_dotenv()
        from app.memory_store import MemoryStore
        from app.config import get_config
        from app.commit_pipeline import MemoryCommitPipeline

        cfg = get_config()
        store = MemoryStore(cfg)
        pipeline = MemoryCommitPipeline()
        result = pipeline.commit_candidate(
            candidate={
                "content": content,
                "memory_type": memory_type,
                "scope": "user",
                "confidence": 0.6,
                "importance": 0.5,
            },
            source_key="web_capture",
            source_type="web",
            source_user_id=current_user.username,
            visibility=visibility,
        )
        atom_id = result.committed_atom_id
        flash(
            f"Captured {'(kept private)' if make_private else '— public and will appear in discussions'}.",
            "success",
        )
    except Exception as exc:
        flash(f"Error saving: {exc}", "error")

    return redirect(url_for("webapp.brain"))


# ── Social routes ─────────────────────────────────────────────────────────────

@site_bp.route("/feed")
def feed():
    format_filter = request.args.get("format", "").strip() or None
    tag_filter = request.args.get("tag", "").strip() or None
    posts = []
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                params: list = []
                where = ["status = 'published'"]
                if format_filter:
                    where.append("format = %s")
                    params.append(format_filter)
                if tag_filter:
                    where.append("%s = ANY(topic_tags)")
                    params.append(tag_filter)
                where_sql = " AND ".join(where)
                params.append(30)
                cur.execute(
                    f"""
                    SELECT sp.id, sp.title, sp.format, sp.topic_tags,
                           sp.confidence_at_publish, sp.published_at,
                           u.username AS author
                    FROM social_posts sp
                    LEFT JOIN users u ON u.id = sp.author_user_id
                    WHERE {where_sql}
                    ORDER BY sp.published_at DESC
                    LIMIT %s;
                    """,
                    params,
                )
                rows = cur.fetchall()
                posts = [
                    {
                        "id": str(r[0]),
                        "title": r[1],
                        "format": r[2],
                        "topic_tags": r[3] or [],
                        "confidence": round(float(r[4] or 0), 2),
                        "published_at": r[5].strftime("%Y-%m-%d") if r[5] else "—",
                        "author": r[6] or "anonymous",
                    }
                    for r in rows
                ]
    except Exception:
        pass
    return render_template(
        "site/feed.html",
        posts=posts,
        format_filter=format_filter or "",
        tag_filter=tag_filter or "",
    )


@site_bp.route("/post/<uuid:post_id>")
def post_detail(post_id):
    post = None
    contributors = []
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT sp.id, sp.title, sp.body, sp.format, sp.topic_tags,
                           sp.confidence_at_publish, sp.published_at, sp.status,
                           u.username AS author
                    FROM social_posts sp
                    LEFT JOIN users u ON u.id = sp.author_user_id
                    WHERE sp.id = %s;
                    """,
                    (str(post_id),),
                )
                r = cur.fetchone()
                if r:
                    post = {
                        "id": str(r[0]),
                        "title": r[1],
                        "body": r[2],
                        "format": r[3],
                        "topic_tags": r[4] or [],
                        "confidence": round(float(r[5] or 0), 2),
                        "published_at": r[6].strftime("%Y-%m-%d") if r[6] else None,
                        "status": r[7],
                        "author": r[8] or "anonymous",
                    }
                cur.execute(
                    """
                    SELECT u.username, pc.contribution_type, pc.quote
                    FROM post_contributors pc
                    LEFT JOIN users u ON u.id = pc.user_id
                    WHERE pc.post_id = %s
                    ORDER BY pc.created_at;
                    """,
                    (str(post_id),),
                )
                contributors = [
                    {"username": r[0] or "anonymous", "type": r[1], "quote": r[2]}
                    for r in cur.fetchall()
                ]
    except Exception:
        pass
    if not post:
        return "Post not found", 404
    return render_template("site/post_detail.html", post=post, contributors=contributors)


@site_bp.route("/post/<uuid:post_id>/publish", methods=["POST"])
@login_required
def publish_post(post_id):
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE social_posts
                    SET status = 'published', published_at = now()
                    WHERE id = %s AND author_user_id = (
                        SELECT id FROM users WHERE username = %s
                    ) AND status = 'draft';
                    """,
                    (str(post_id), current_user.username),
                )
            conn.commit()
    except Exception:
        pass
    flash("Post published.", "success")
    return redirect(url_for("webapp.post_detail", post_id=post_id))


@site_bp.route("/post/<uuid:post_id>/discard", methods=["POST"])
@login_required
def discard_post(post_id):
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE social_posts SET status = 'archived'
                    WHERE id = %s AND author_user_id = (
                        SELECT id FROM users WHERE username = %s
                    );
                    """,
                    (str(post_id), current_user.username),
                )
            conn.commit()
    except Exception:
        pass
    flash("Post discarded.", "success")
    return redirect(url_for("webapp.drafts"))


@site_bp.route("/drafts")
@login_required
def drafts():
    posts = []
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT sp.id, sp.title, sp.format, sp.confidence_at_publish, sp.created_at
                    FROM social_posts sp
                    JOIN users u ON u.id = sp.author_user_id
                    WHERE u.username = %s AND sp.status = 'draft'
                    ORDER BY sp.created_at DESC;
                    """,
                    (current_user.username,),
                )
                rows = cur.fetchall()
                posts = [
                    {
                        "id": str(r[0]),
                        "title": r[1],
                        "format": r[2],
                        "confidence": round(float(r[3] or 0), 2),
                        "created_at": r[4].strftime("%Y-%m-%d %H:%M") if r[4] else "—",
                    }
                    for r in rows
                ]
    except Exception:
        pass
    return render_template("site/drafts.html", posts=posts)


@site_bp.route("/connections")
@login_required
def connections():
    from app.topic_matcher import find_connections_for_user
    matches = []
    try:
        matches = find_connections_for_user(username=current_user.username, limit=15)
    except Exception:
        pass
    return render_template("site/connections.html", matches=matches)


# ── Admin view ────────────────────────────────────────────────────────────────

@site_bp.route("/admin")
@login_required
def admin():
    if not current_user.is_admin:
        return "Forbidden", 403

    vis_filter = request.args.get("vis", "")
    type_filter = request.args.get("type", "")
    user_filter = request.args.get("user", "")
    page = max(1, int(request.args.get("page", 1)))
    per_page = 50
    offset = (page - 1) * per_page

    atoms = []
    stats = {}
    users = []
    total = 0

    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                # Global stats
                cur.execute("""
                    SELECT
                        COUNT(*) FILTER (WHERE lifecycle_status='active'),
                        COUNT(*) FILTER (WHERE visibility='public' AND lifecycle_status='active'),
                        COUNT(*) FILTER (WHERE visibility='private' AND lifecycle_status='active'),
                        COUNT(*) FILTER (WHERE interest_flag=true AND lifecycle_status='active')
                    FROM memory_atoms;
                """)
                r = cur.fetchone()
                stats = {"total": r[0], "public": r[1], "private": r[2], "novel": r[3]}

                cur.execute("SELECT COUNT(*) FROM discussions;")
                stats["discussions"] = cur.fetchone()[0]

                # Resolution rate: % of discussions that reached answered/validated
                cur.execute(
                    """
                    SELECT
                        COUNT(*) FILTER (WHERE thread_status IN ('answered', 'validated')) AS resolved,
                        COUNT(*) FILTER (WHERE thread_status IN ('gathering', 'updated', 'unresolved', 'reopened')) AS pending,
                        COUNT(*) AS total
                    FROM discussions;
                    """
                )
                res = cur.fetchone()
                resolved, pending, disc_total = (res[0] or 0), (res[1] or 0), (res[2] or 1)
                stats["resolved"] = resolved
                stats["pending"] = pending
                stats["resolution_rate"] = round(100 * resolved / max(disc_total, 1), 1)
                stats["disc_status"] = {
                    r[0]: r[1]
                    for r in cur.fetchmany(0) or []  # cleared by fetchone above — use stats
                }

                cur.execute("SELECT COUNT(*) FROM users;")
                stats["users"] = cur.fetchone()[0]

                # User list for filter
                cur.execute("SELECT DISTINCT source_user_id FROM memory_signals ORDER BY source_user_id;")
                users = [r[0] for r in cur.fetchall()]

                # Build filter
                where = ["ma.lifecycle_status = 'active'"]
                params: list = []
                if vis_filter:
                    where.append("ma.visibility = %s"); params.append(vis_filter)
                if type_filter:
                    where.append("ma.memory_type = %s"); params.append(type_filter)
                if user_filter:
                    where.append("ms.source_user_id = %s"); params.append(user_filter)

                where_sql = " AND ".join(where)
                count_params = params.copy()
                params += [per_page, offset]

                cur.execute(f"""
                    SELECT COUNT(DISTINCT ma.id)
                    FROM memory_atoms ma
                    LEFT JOIN memory_signals ms ON ms.memory_atom_id = ma.id
                    WHERE {where_sql};
                """, count_params)
                total = cur.fetchone()[0]

                cur.execute(f"""
                    SELECT DISTINCT ma.id, ma.content, ma.memory_type, ma.scope,
                           ma.confidence, ma.visibility, ma.interest_flag,
                           ma.novelty_score, ma.created_at, ms.source_user_id
                    FROM memory_atoms ma
                    LEFT JOIN memory_signals ms ON ms.memory_atom_id = ma.id
                    WHERE {where_sql}
                    ORDER BY ma.created_at DESC
                    LIMIT %s OFFSET %s;
                """, params)
                atoms = [
                    {
                        "id": str(r[0]), "content": r[1], "memory_type": r[2],
                        "scope": r[3] or "—", "confidence": round(float(r[4]), 2),
                        "visibility": r[5], "interest_flag": r[6],
                        "novelty_score": round(float(r[7] or 0), 2),
                        "created_at": r[8].strftime("%Y-%m-%d %H:%M") if r[8] else "—",
                        "source_user": r[9] or "—",
                    }
                    for r in cur.fetchall()
                ]
    except Exception as exc:
        flash(f"DB error: {exc}", "error")

    total_pages = max(1, (total + per_page - 1) // per_page)
    return render_template("site/admin.html",
        atoms=atoms, stats=stats, users=users, total=total,
        page=page, total_pages=total_pages,
        vis_filter=vis_filter, type_filter=type_filter, user_filter=user_filter)


@site_bp.route("/admin/atom/<uuid:atom_id>/visibility", methods=["POST"])
@login_required
def admin_toggle_visibility(atom_id):
    if not current_user.is_admin:
        return "Forbidden", 403
    new_vis = request.form.get("visibility", "private")
    if new_vis not in ("private", "public"):
        new_vis = "private"
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE memory_atoms SET visibility = %s WHERE id = %s;",
                    (new_vis, str(atom_id)),
                )
            conn.commit()
    except Exception:
        pass
    return redirect(request.referrer or url_for("webapp.admin"))


# ── Graph data API ────────────────────────────────────────────────────────────

@site_bp.route("/api/graph")
def api_graph():
    from flask import jsonify
    nodes = []
    links = []
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id, title, topic_tags, atom_count, contributor_count,
                           novelty_flag, last_activity_at
                    FROM discussions ORDER BY atom_count DESC LIMIT 200;
                """)
                rows = cur.fetchall()

        # Build node list
        tag_to_ids: dict[str, list[str]] = {}
        for r in rows:
            nid = str(r[0])
            tags = r[2] or []
            nodes.append({
                "id": nid,
                "title": r[1],
                "atom_count": r[3] or 1,
                "contributor_count": r[4] or 0,
                "novelty_flag": bool(r[5]),
                "last_activity": _ago(r[6]),
            })
            for tag in tags:
                tag_to_ids.setdefault(tag, []).append(nid)

        # Edges: discussions sharing a topic_tag
        seen: set[tuple] = set()
        for tag, ids in tag_to_ids.items():
            for i in range(len(ids)):
                for j in range(i + 1, len(ids)):
                    a, b = ids[i], ids[j]
                    key = (min(a, b), max(a, b))
                    if key not in seen:
                        seen.add(key)
                        links.append({"source": a, "target": b, "tag": tag})

    except Exception:
        pass

    return jsonify({"nodes": nodes, "links": links})


@site_bp.route("/discussions/graph")
def discussion_graph():
    return render_template("site/discussion_graph.html")


# ── MCP over HTTP — used by npm bridge (Claude Desktop) ──────────────────────

@site_bp.route("/mcp/sse", methods=["GET", "POST"])
def mcp_sse():
    """MCP tool dispatcher for the npx memory-layer bridge.

    POST: {"tool": "<name>", "args": {...}}
          → runs the tool, returns {"result": {...}} or {"error": "..."}
    GET:  health check / discovery.

    Auth: Authorization: Bearer <api_token>
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return jsonify({"error": "Authorization: Bearer <api_token> required"}), 401
    token = auth_header[7:].strip()
    username = _resolve_api_user(token)
    if not username:
        return jsonify({"error": "Invalid or inactive api_token"}), 401

    if request.method == "GET":
        return jsonify({"status": "ok", "server": "memoryLayer", "user": username})

    body = request.get_json(silent=True) or {}
    tool = body.get("tool", "")
    args = body.get("args", {})

    try:
        result = _dispatch_mcp_tool(tool, args, username)
        return jsonify({"result": result})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


def _dispatch_mcp_tool(tool: str, args: dict, username: str):
    """Route a tool name + args to the appropriate backend function."""
    if tool == "memory_health":
        from mcp_server.tools.health import get_memory_health
        return get_memory_health()

    if tool == "memory_search":
        from mcp_server.tools.search import search_memories
        return search_memories(
            query=args.get("query", ""),
            limit=int(args.get("limit", 5)),
            scope=args.get("scope"),
            memory_type=args.get("memory_type"),
            min_similarity=float(args.get("min_similarity", 0.0)),
        )

    if tool == "memory_store_auto":
        from mcp_server.tools.store_auto import store_memory_auto
        return store_memory_auto(
            content=args.get("content", ""),
            memory_type=args.get("memory_type", "observation"),
            relationship=args.get("relationship", "new"),
            context_summary=args.get("context_summary"),
            scope=args.get("scope"),
            confidence=float(args.get("confidence", 0.8)),
            importance=float(args.get("importance", 0.5)),
            reconciliation_reason=args.get("reconciliation_reason"),
            matched_memory_ids=args.get("matched_memory_ids"),
            source_user_id=username,
            visibility=args.get("visibility", "public"),
        )

    if tool == "memory_get":
        from mcp_server.tools.get import get_memory_by_id
        return get_memory_by_id(args.get("memory_id", ""))

    if tool == "memory_task_context":
        from mcp_server.tools.task_context import get_task_context
        return get_task_context(
            project_scope=args.get("project_scope", "user"),
            model_scope=args.get("model_scope"),
            task_hint=args.get("task_hint"),
            recent_tasks=int(args.get("recent_tasks", 5)),
            compact=bool(args.get("compact", True)),
        )

    if tool == "memory_audit":
        from mcp_server.tools.health import get_memory_health
        from mcp_server.tools.stale_atoms import get_stale_atoms
        from mcp_server.tools.find_duplicates import find_duplicate_atoms
        health = get_memory_health()
        stale = get_stale_atoms(days_threshold=int(args.get("stale_days", 90)), scope=args.get("scope"), limit=20)
        dupes = find_duplicate_atoms(similarity_threshold=float(args.get("duplicate_threshold", 0.90)), scope=args.get("scope"), limit=20)
        return {"health": health, "stale_atoms": stale, "duplicate_pairs": dupes.get("pairs", []) if isinstance(dupes, dict) else dupes}

    if tool == "memory_link_atoms":
        from mcp_server.tools.link_atoms import link_atoms
        return link_atoms(
            atom_a_id=args.get("atom_a_id", ""),
            atom_b_id=args.get("atom_b_id", ""),
            relation_type=args.get("relation_type", "related"),
            confidence=float(args.get("confidence", 0.8)),
        )

    if tool == "memory_related":
        from mcp_server.tools.related_atoms import get_related_atoms
        return get_related_atoms(
            atom_id=args.get("atom_id", ""),
            depth=int(args.get("depth", 1)),
            relation_types=args.get("relation_types"),
        )

    raise ValueError(f"Unknown tool: {tool}")


# ── REST ingest API — proper channel for Ollama / external LLMs ───────────────

def _resolve_api_user(token: str) -> str | None:
    """Return username for a valid api_token, else None."""
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT username FROM users WHERE api_token = %s AND is_active = true LIMIT 1;",
                    (token,),
                )
                row = cur.fetchone()
                return row[0] if row else None
    except Exception:
        return None


@site_bp.route("/api/ingest", methods=["POST"])
def api_ingest():
    """HTTP ingest endpoint for Ollama / external LLMs.

    Auth: Authorization: Bearer <api_token>
    Body (JSON):
        content       str   required
        memory_type   str   optional (default: observation)
        visibility    str   optional (default: public)
        scope         str   optional (default: user)
        source        str   optional (label for the originating tool/model)
    Returns: JSON write report with atom_id, signal_id, decision, quality_score.
    """
    # ── Auth ──
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return jsonify({"error": "Missing Authorization: Bearer <api_token>"}), 401
    token = auth_header[7:].strip()
    username = _resolve_api_user(token)
    if not username:
        return jsonify({"error": "Invalid or inactive api_token"}), 401

    # ── Parse body ──
    body = request.get_json(silent=True) or {}
    content = (body.get("content") or "").strip()
    if not content:
        return jsonify({"error": "content is required"}), 400

    memory_type = body.get("memory_type", "observation")
    visibility = body.get("visibility", "public")
    scope = body.get("scope") or "user"
    source_label = body.get("source", "api")

    if visibility not in ("public", "private", "team"):
        visibility = "public"

    # ── Commit pipeline ──
    try:
        from app.commit_pipeline import MemoryCommitPipeline
        pipeline = MemoryCommitPipeline()
        result = pipeline.commit_candidate(
            content=content,
            memory_type=memory_type,
            source_key=f"{source_label}:{username}",
            caller_id=username,
            visibility=visibility,
            scope=scope,
        )
        return jsonify({
            "stored": result.get("stored", False),
            "decision": result.get("decision"),
            "atom_id": result.get("memory_atom_id"),
            "signal_id": result.get("memory_signal_id"),
            "quality_score": result.get("quality_score"),
            "content": result.get("content"),
        }), 200 if result.get("stored") else 202
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ── Discussion routes ─────────────────────────────────────────────────────────

@site_bp.route("/explore")
@login_required
def explore():
    """Public feed of auto-published discussions, ranked by user topic affinity.

    For users with conversation history the feed is re-ranked so discussions
    matching their atom corpus appear first. For new users with no history
    the feed falls back to pure recency — broadcast always works.
    """
    rows = []
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT d.id, d.title, d.topic_tags, d.contributor_count,
                           d.atom_count, d.thread_status, d.last_activity_at,
                           d.summary, d.novelty_flag
                    FROM discussions d
                    WHERE d.auto_published = true
                    ORDER BY d.last_activity_at DESC
                    LIMIT 100;
                    """
                )
                for r in cur.fetchall():
                    rows.append({
                        "id": str(r[0]),
                        "title": r[1],
                        "topic_tags": r[2] or [],
                        "contributor_count": r[3],
                        "atom_count": r[4],
                        "thread_status": r[5] or "gathering",
                        "last_activity": _ago(r[6]),
                        "summary": r[7] or "",
                        "novelty_flag": r[8],
                    })
    except Exception:
        pass

    # Re-rank by topic affinity if the user has history; broadcast fallback if not
    user_tags: list[str] = []
    if current_user.is_authenticated and rows:
        try:
            from app.topic_affinity import get_user_topic_tags, rank_discussions_by_affinity
            import os as _os
            user_tags = get_user_topic_tags(
                current_user.username, _os.environ.get("DATABASE_URL", "")
            )
            rows = rank_discussions_by_affinity(rows, user_tags)
        except Exception:
            pass  # silently fall back to recency order

    return render_template("site/explore.html", discussions=rows, user_tags=user_tags)


@site_bp.route("/categories")
@login_required
def categories():
    """Browse discussions by topic tag — no profile required."""
    selected = request.args.get("tag", "").strip().lower()
    all_tags: list[str] = []
    tag_counts: dict[str, int] = {}
    discussions: list[dict] = []

    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                # Aggregate all topic tags across published discussions
                cur.execute(
                    """
                    SELECT unnest(topic_tags) AS tag, COUNT(*) AS cnt
                    FROM discussions
                    WHERE auto_published = true
                    GROUP BY tag
                    ORDER BY cnt DESC, tag ASC
                    LIMIT 60;
                    """
                )
                for r in cur.fetchall():
                    tag_counts[r[0]] = r[1]
                    all_tags.append(r[0])

                if selected and selected in tag_counts:
                    cur.execute(
                        """
                        SELECT d.id, d.title, d.topic_tags, d.contributor_count,
                               d.atom_count, d.thread_status, d.last_activity_at,
                               d.summary, d.novelty_flag
                        FROM discussions d
                        WHERE d.auto_published = true
                          AND %s = ANY(d.topic_tags)
                        ORDER BY d.last_activity_at DESC
                        LIMIT 50;
                        """,
                        (selected,),
                    )
                    for r in cur.fetchall():
                        discussions.append({
                            "id": str(r[0]),
                            "title": r[1],
                            "topic_tags": r[2] or [],
                            "contributor_count": r[3],
                            "atom_count": r[4],
                            "thread_status": r[5] or "gathering",
                            "last_activity": _ago(r[6]),
                            "summary": r[7] or "",
                            "novelty_flag": r[8],
                        })
    except Exception:
        pass

    return render_template(
        "site/categories.html",
        all_tags=all_tags,
        tag_counts=tag_counts,
        selected=selected,
        discussions=discussions,
    )


@site_bp.route("/search")
@login_required
def search():
    """Find existing discussions before creating a new one — reduces duplicates."""
    query = request.args.get("q", "").strip()
    results: list[dict] = []
    atom_results: list[dict] = []

    if query:
        try:
            with _conn() as conn:
                with conn.cursor() as cur:
                    # Full-text search on discussion title + summary
                    cur.execute(
                        """
                        SELECT d.id, d.title, d.topic_tags, d.contributor_count,
                               d.atom_count, d.thread_status, d.last_activity_at,
                               d.summary
                        FROM discussions d
                        WHERE d.auto_published = true
                          AND (d.title ILIKE %s OR d.summary ILIKE %s
                               OR %s = ANY(d.topic_tags))
                        ORDER BY d.contributor_count DESC, d.last_activity_at DESC
                        LIMIT 20;
                        """,
                        (f"%{query}%", f"%{query}%", query.lower()),
                    )
                    for r in cur.fetchall():
                        results.append({
                            "id": str(r[0]),
                            "title": r[1],
                            "topic_tags": r[2] or [],
                            "contributor_count": r[3],
                            "atom_count": r[4],
                            "thread_status": r[5] or "gathering",
                            "last_activity": _ago(r[6]),
                            "summary": r[7] or "",
                        })
        except Exception:
            pass

        # Also search memory atoms (semantic — finds related knowledge)
        try:
            from app.topic_affinity import get_user_topic_tags
            import os as _os
            db_url = _os.environ.get("DATABASE_URL", "")
            store = __import__("app.db", fromlist=["get_store"]).get_store()
            atom_rows = store.search_memories_full(
                query=query, limit=5, min_similarity=0.45
            )
            for a in atom_rows:
                atom_results.append({
                    "id": str(a.get("id", "")),
                    "content": (a.get("context_summary") or a.get("content", ""))[:300],
                    "memory_type": a.get("memory_type", ""),
                    "confidence": round(float(a.get("confidence", 0)), 2),
                    "similarity": round(float(a.get("similarity", 0)), 3),
                })
        except Exception:
            pass

    return render_template(
        "site/search.html",
        query=query,
        results=results,
        atom_results=atom_results,
    )


@site_bp.route("/discussions")
@login_required
def discussions():
    rows = []
    unread_total = 0
    status_filter = request.args.get("status", "").strip().lower()
    valid_statuses = {"gathering", "updated", "answered", "validated", "reopened", "unresolved"}
    if status_filter not in valid_statuses:
        status_filter = ""
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                base_sql = """
                    SELECT d.id, d.title, d.topic_tags, d.contributor_count,
                           d.atom_count, d.novelty_flag, d.last_activity_at,
                           COALESCE(SUM(n.new_atom_count) FILTER (WHERE n.read = false), 0) AS unread,
                           d.thread_status
                    FROM discussions d
                    JOIN discussion_atoms da ON da.discussion_id = d.id
                    LEFT JOIN user_notifications n
                        ON n.discussion_id = d.id
                        AND n.user_id = (SELECT id FROM users WHERE username = %s)
                    WHERE da.source_user_id = %s
                    {status_clause}
                    GROUP BY d.id
                    ORDER BY d.last_activity_at DESC
                    LIMIT 50;
                """
                if status_filter:
                    sql = base_sql.format(status_clause="AND d.thread_status = %s")
                    params = (current_user.username, current_user.username, status_filter)
                else:
                    sql = base_sql.format(status_clause="")
                    params = (current_user.username, current_user.username)
                cur.execute(sql, params)
                for r in cur.fetchall():
                    unread = int(r[7])
                    unread_total += unread
                    rows.append({
                        "id": str(r[0]),
                        "title": r[1],
                        "topic_tags": r[2] or [],
                        "contributor_count": r[3],
                        "atom_count": r[4],
                        "novelty_flag": r[5],
                        "last_activity": _ago(r[6]),
                        "unread": unread,
                        "thread_status": r[8] or "active",
                    })
    except Exception:
        pass
    return render_template("site/discussions.html",
                           discussions=rows, unread_total=unread_total,
                           status_filter=status_filter)


@site_bp.route("/discussion/<uuid:disc_id>")
@login_required
def discussion_detail(disc_id):
    disc = None
    contributions = []
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, title, topic_tags, contributor_count, novelty_flag, "
                    "last_activity_at, thread_status "
                    "FROM discussions WHERE id = %s;",
                    (str(disc_id),),
                )
                r = cur.fetchone()
                if r:
                    disc = {
                        "id": str(r[0]), "title": r[1], "topic_tags": r[2] or [],
                        "contributor_count": r[3], "novelty_flag": r[4],
                        "last_activity": r[5].strftime("%Y-%m-%d") if r[5] else "—",
                        "thread_status": r[6] or "active",
                    }
                cur.execute(
                    """
                    SELECT ma.content, ma.memory_type, ma.confidence,
                           da.novelty_score, da.source_user_id, da.added_at,
                           ma.topic_tags
                    FROM discussion_atoms da
                    JOIN memory_atoms ma ON ma.id = da.atom_id
                    WHERE da.discussion_id = %s
                    ORDER BY da.novelty_score DESC, da.added_at ASC
                    LIMIT 100;
                    """,
                    (str(disc_id),),
                )
                for r in cur.fetchall():
                    is_mine = r[4] == current_user.username
                    contributions.append({
                        "content": r[0],
                        "memory_type": r[1],
                        "confidence": round(float(r[2]), 2),
                        "novelty_score": round(float(r[3]), 2),
                        "is_mine": is_mine,
                        "attribution": "you" if is_mine else "anonymous contributor",
                        "added_at": r[5].strftime("%Y-%m-%d") if r[5] else "—",
                        "topic_tags": r[6] or [],
                    })
                # Check if any linked atom has high disagreement (flagged for revision)
                cur.execute(
                    """
                    SELECT 1 FROM discussion_atoms da
                    JOIN memory_atoms ma ON ma.id = da.atom_id
                    WHERE da.discussion_id = %s AND ma.disagreement_score > 0.5
                    LIMIT 1;
                    """,
                    (str(disc_id),),
                )
                revision_flag = cur.fetchone() is not None
                if disc:
                    disc["revision_flag"] = revision_flag
                # Mark notifications read
                cur.execute(
                    """
                    UPDATE user_notifications SET read = true
                    WHERE discussion_id = %s
                      AND user_id = (SELECT id FROM users WHERE username = %s);
                    """,
                    (str(disc_id), current_user.username),
                )
            conn.commit()
    except Exception:
        pass
    if not disc:
        return "Discussion not found", 404
    # Related discussions by topic_tags overlap
    related = []
    if disc.get("topic_tags"):
        try:
            with _conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT id, title, contributor_count, atom_count, last_activity_at
                        FROM discussions
                        WHERE id != %s AND topic_tags && %s
                        ORDER BY last_activity_at DESC LIMIT 5;
                        """,
                        (str(disc_id), disc["topic_tags"]),
                    )
                    related = [
                        {"id": str(r[0]), "title": r[1],
                         "contributor_count": r[2], "atom_count": r[3],
                         "last_activity": _ago(r[4])}
                        for r in cur.fetchall()
                    ]
        except Exception:
            pass
    return render_template("site/discussion_detail.html", disc=disc,
                           contributions=contributions, related=related)


@site_bp.route("/discussion/<uuid:disc_id>/react", methods=["POST"])
@login_required
def discussion_react(disc_id):
    """Accept a perspective from any browsing user and feed it into the pipeline.

    The perspective is committed as a memory atom through the full write pipeline
    (quality → reconcile → critic → risk gate → dual-write). The new atom is
    linked to the discussion. The discussion's contributor count and
    last_activity_at are updated. A notification is queued for the originating
    user — they receive a synthesis later, not the raw text.
    """
    perspective = (request.form.get("perspective") or "").strip()
    if not perspective or len(perspective) < 10:
        flash("Perspective is too short.", "error")
        return redirect(url_for("webapp.discussion_detail", disc_id=disc_id))

    try:
        from app.commit_pipeline import MemoryCommitPipeline

        pipeline = MemoryCommitPipeline()
        candidate = {
            "content": perspective,
            "memory_type": "observation",
            "scope": f"discussion:{disc_id}",
            "importance": 0.6,
            "should_store": True,
        }
        decision = pipeline.commit_candidate(
            candidate,
            source_key=current_user.username,
            source_type="user_reaction",
            source_user_id=current_user.username,
        )
        atom_id = decision.committed_atom_id

        if atom_id:
            with _conn() as conn:
                with conn.cursor() as cur:
                    # Link new atom to discussion
                    cur.execute(
                        """
                        INSERT INTO discussion_atoms
                            (discussion_id, atom_id, source_user_id, novelty_score)
                        VALUES (%s, %s, %s, 0.5)
                        ON CONFLICT (discussion_id, atom_id) DO NOTHING;
                        """,
                        (str(disc_id), atom_id, current_user.username),
                    )
                    # Update discussion activity and contributor count.
                    # If already answered/validated, reopen — new evidence arrived.
                    cur.execute(
                        """
                        UPDATE discussions
                        SET last_activity_at = now(),
                            contributor_count = contributor_count + 1,
                            atom_count = atom_count + 1,
                            thread_status = CASE
                                WHEN thread_status IN ('answered', 'validated') THEN 'reopened'
                                ELSE thread_status
                            END
                        WHERE id = %s
                        RETURNING thread_status;
                        """,
                        (str(disc_id),),
                    )
                    new_status = (cur.fetchone() or [None])[0]
                    # Reopened → fire an immediate notification for the creator
                    if new_status == "reopened":
                        cur.execute(
                            """
                            INSERT INTO user_notifications
                                (user_id, discussion_id, new_atom_count, notification_type)
                            SELECT d.created_by_user_id, d.id, 1, 'reopened'
                            FROM discussions d
                            WHERE d.id = %s
                              AND d.created_by_user_id IS NOT NULL;
                            """,
                            (str(disc_id),),
                        )
                    # Queue notification for the discussion creator (not the reactor)
                    cur.execute(
                        """
                        INSERT INTO user_notifications
                            (user_id, discussion_id, new_atom_count)
                        SELECT d.created_by_user_id, d.id, 1
                        FROM discussions d
                        WHERE d.id = %s
                          AND d.created_by_user_id IS NOT NULL
                          AND d.created_by_user_id != (SELECT id FROM users WHERE username = %s);
                        """,
                        (str(disc_id), current_user.username),
                    )
                conn.commit()
            # Trigger synthesis in background — non-blocking
            from concurrent.futures import ThreadPoolExecutor
            from app.discussion_synthesizer import synthesise_discussion
            import os as _os
            _db_url = _os.environ.get("DATABASE_URL", "")
            _disc_id_str = str(disc_id)
            ThreadPoolExecutor(max_workers=1).submit(
                synthesise_discussion, _disc_id_str, _db_url
            )
            flash("Your perspective has been added.", "success")
        else:
            flash("Your perspective was noted but didn't produce new insight.", "info")
    except Exception as exc:
        flash("Something went wrong. Please try again.", "error")
        _logger.warning("discussion_react: %s", exc)

    return redirect(url_for("webapp.discussion_detail", disc_id=disc_id))


@site_bp.route("/discussion/<uuid:disc_id>/helpful", methods=["POST"])
@login_required
def discussion_helpful(disc_id):
    """Mark an answered discussion as helpful — advances status to Validated.

    Increases confidence on the linked synthesis atom. Only valid when
    thread_status is 'answered'. Creator or any contributor can mark it.
    """
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                # Only advance if currently answered
                cur.execute(
                    """
                    UPDATE discussions
                    SET thread_status = 'validated', last_activity_at = now()
                    WHERE id = %s AND thread_status = 'answered'
                    RETURNING id;
                    """,
                    (str(disc_id),),
                )
                updated = cur.fetchone()
                if updated:
                    # Boost confidence on all atoms linked to this discussion
                    cur.execute(
                        """
                        UPDATE memory_atoms ma
                        SET confidence = LEAST(confidence + 0.08, 1.0),
                            support_weight = support_weight + 1
                        FROM discussion_atoms da
                        WHERE da.discussion_id = %s AND da.atom_id = ma.id;
                        """,
                        (str(disc_id),),
                    )
                    # Increment usefulness_score for all contributors — prior outcomes tracking
                    cur.execute(
                        """
                        UPDATE users u
                        SET usefulness_score = usefulness_score + 0.1
                        FROM discussion_atoms da
                        WHERE da.discussion_id = %s
                          AND da.source_user_id = u.username;
                        """,
                        (str(disc_id),),
                    )
            conn.commit()
        flash("Marked as helpful — the knowledge was confirmed.", "success")
    except Exception as exc:
        _logger.warning("discussion_helpful: %s", exc)
        flash("Could not record your rating.", "error")
    return redirect(url_for("webapp.discussion_detail", disc_id=disc_id))


@site_bp.route("/notifications")
@login_required
def notifications():
    """Show the user what changed on conversations they care about."""
    items = []
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT n.id, d.id, d.title, d.thread_status,
                           n.new_atom_count, n.read, n.created_at,
                           COALESCE(n.notification_type, 'reaction')
                    FROM user_notifications n
                    JOIN discussions d ON d.id = n.discussion_id
                    WHERE n.user_id = (SELECT id FROM users WHERE username = %s)
                    ORDER BY n.created_at DESC
                    LIMIT 50;
                    """,
                    (current_user.username,),
                )
                for r in cur.fetchall():
                    status = r[3] or "active"
                    notif_type = r[7]
                    if notif_type == "published":
                        message = "Your conversation was shared with people who may know more."
                    elif notif_type == "reopened" or status == "reopened":
                        message = "New information arrived on your conversation."
                    elif status == "answered":
                        message = "Your conversation has a new answer."
                    elif status == "updated":
                        count = r[4]
                        message = f"New perspective{'s' if count != 1 else ''} arrived on your conversation."
                    else:
                        count = r[4]
                        message = f"{count} new perspective{'s' if count != 1 else ''} added."
                    items.append({
                        "id": str(r[0]),
                        "disc_id": str(r[1]),
                        "disc_title": r[2],
                        "thread_status": status,
                        "message": message,
                        "read": r[5],
                        "created_at": _ago(r[6]),
                    })
                # Mark all read
                cur.execute(
                    """
                    UPDATE user_notifications SET read = true
                    WHERE user_id = (SELECT id FROM users WHERE username = %s)
                      AND read = false;
                    """,
                    (current_user.username,),
                )
            conn.commit()
    except Exception:
        pass
    return render_template("site/notifications.html", items=items)


def _unread_notification_count(username: str) -> int:
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COALESCE(SUM(n.new_atom_count), 0)
                    FROM user_notifications n
                    JOIN users u ON u.id = n.user_id
                    WHERE u.username = %s AND n.read = false;
                    """,
                    (username,),
                )
                row = cur.fetchone()
                return int(row[0]) if row else 0
    except Exception:
        return 0


# ── Admin chat ───────────────────────────────────────────────────────────────

_CHAT_HISTORY_LIMIT = 100  # max messages per session (50 turns)


def _session_load(session_id: str | None) -> list[dict]:
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
                cur.execute(
                    "DELETE FROM dashboard_sessions WHERE updated_at < NOW() - INTERVAL '30 days';"
                )
            conn.commit()
    except Exception:
        pass
    return sid


@site_bp.route("/chat", methods=["GET", "POST"])
@login_required
def chat():
    if not current_user.is_admin:
        return "Forbidden", 403

    session_id: str | None = session.get("chat_session_id")
    history: list[dict] = _session_load(session_id)
    error = None

    # Pre-load discussion context when arriving via "Discuss in depth" link
    if request.method == "GET" and not history:
        disc_param = request.args.get("discussion", "").strip()
        if disc_param:
            try:
                with _conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT title, summary, atom_count, contributor_count FROM discussions WHERE id = %s;",
                            (disc_param,),
                        )
                        disc_row = cur.fetchone()
                if disc_row:
                    disc_title, disc_summary, atom_count, contributor_count = disc_row
                    context_msg = (
                        f"I'd like to discuss: **{disc_title}**"
                        + (f"\n\n{disc_summary}" if disc_summary else "")
                        + f"\n\nThis topic has {contributor_count} contributor(s) and {atom_count} idea(s) in the knowledge base. "
                        "What can you tell me about it, and what should I know?"
                    )
                    history = [{"role": "user", "content": context_msg}]
                    # Immediately generate an opening response so the user lands in a live conversation
                    from app.chat import chat_with_research, clean_assistant_response
                    messages_for_llm = [{"role": "user", "content": context_msg}]
                    raw_answer, memories, _, _, _, _, _ = chat_with_research(
                        messages_for_llm, source_user_id=current_user.username
                    )
                    history.append({"role": "assistant", "content": clean_assistant_response(raw_answer), "memories": []})
                    session_id = _session_save(None, history)
                    session["chat_session_id"] = session_id
            except Exception:
                pass  # silently fall through to empty chat if anything fails

    if request.method == "POST":
        message = request.form.get("message", "").strip()
        if message:
            try:
                from app.chat import chat_with_research, clean_assistant_response

                messages_for_llm = [
                    {"role": m["role"], "content": m["content"]}
                    for m in history
                ] + [{"role": "user", "content": message}]

                raw_answer, memories, research_results, research_status, context_eval, gap_state, route = chat_with_research(
                    messages_for_llm,
                    source_user_id=current_user.username,
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

                if len(history) > _CHAT_HISTORY_LIMIT:
                    history = history[-_CHAT_HISTORY_LIMIT:]

                session_id = _session_save(session_id, history)
                session["chat_session_id"] = session_id
            except Exception as exc:
                error = str(exc)
        else:
            error = "Please enter a message."

    return render_template("site/chat.html", history=history, error=error)


@site_bp.route("/chat/clear", methods=["POST"])
@login_required
def chat_clear():
    if not current_user.is_admin:
        return "Forbidden", 403
    session_id = session.pop("chat_session_id", None)
    if session_id:
        try:
            with _conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM dashboard_sessions WHERE id = %s;",
                        (session_id,),
                    )
                conn.commit()
        except Exception:
            pass
    return redirect(url_for("webapp.chat"))


# ── Health (for Railway/Render healthcheck) ───────────────────────────────────

@site_bp.route("/health")
def health():
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1;")
        return {"status": "ok"}, 200
    except Exception as exc:
        return {"status": "error", "detail": str(exc)}, 500
