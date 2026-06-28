"""Public site Blueprint — landing, auth, user brain dashboard, settings."""
from __future__ import annotations

import json
import uuid
from urllib.parse import quote as _urlquote

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

_CATEGORIES = {
    "sci-tech": {
        "label": "Sci / Tech",
        "keywords": {"technology","science","ai","software","health","space","climate","data",
                     "programming","engineering","medicine","biology","physics","computing",
                     "machine learning","robotics","crypto","cybersecurity","startup"},
    },
    "entertainment": {
        "label": "Entertainment",
        "keywords": {"music","film","movies","tv","gaming","games","art","comedy","celebrity",
                     "streaming","books","literature","theater","fashion","design","anime",
                     "podcast","photography","creative"},
    },
    "news": {
        "label": "News",
        "keywords": {"politics","world","government","policy","election","law","crime",
                     "economy","finance","business","market","war","international",
                     "current events","society","environment","education"},
    },
    "sports": {
        "label": "Sports",
        "keywords": {"sports","football","basketball","baseball","soccer","tennis","golf",
                     "hockey","olympics","fitness","athletics","racing","esports","cycling"},
    },
}

def _categorize_post(topic_tags: list[str]) -> str:
    """Return best-matching category key for a post's topic_tags, or 'other'."""
    tags_lower = {t.lower() for t in topic_tags}
    best, best_score = "other", 0
    for key, cat in _CATEGORIES.items():
        score = len(tags_lower & cat["keywords"])
        if score > best_score:
            best, best_score = key, score
    return best


@site_bp.route("/")
def landing():
    _VALID_SORTS = ("for_you", "new", "popular", "rising")
    sort = request.args.get("sort", "for_you" if current_user.is_authenticated else "new").strip()
    if sort not in _VALID_SORTS:
        sort = "for_you" if current_user.is_authenticated else "new"
    category = request.args.get("category", "").strip()
    if category not in _CATEGORIES:
        category = ""

    draft_posts = []
    last_seen_at = None
    user_atom_count = 0

    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                if current_user.is_authenticated:
                    cur.execute("SELECT last_seen_at FROM users WHERE username = %s;",
                                (current_user.username,))
                    row = cur.fetchone()
                    last_seen_at = row[0] if row else None
                    cur.execute(
                        "SELECT COUNT(*) FROM memory_atoms ma JOIN memory_signals ms ON ms.memory_atom_id = ma.id WHERE ms.source_user_id = %s AND ma.lifecycle_status = 'active';",
                        (current_user.username,),
                    )
                    cnt_row = cur.fetchone()
                    user_atom_count = int(cnt_row[0]) if cnt_row else 0
                    cur.execute("UPDATE users SET last_seen_at = now() WHERE username = %s;",
                                (current_user.username,))
                    conn.commit()
    except Exception:
        pass

    try:
        from app.feed_ranker import get_personalized_feed  # noqa: PLC0415
        cat_kws = list(_CATEGORIES[category]["keywords"]) if category else None
        username = current_user.username if current_user.is_authenticated else None
        raw_rows = get_personalized_feed(username=username, category_keywords=cat_kws, sort=sort)
        all_posts = [
            {
                "id": str(r[0]),
                "title": r[1],
                "format": r[2],
                "topic_tags": r[3] or [],
                "published_at": _ago(r[4]),
                "author": r[5] or "anonymous",
                "excerpt": r[6] or "",
                "perspective_count": r[7] or 0,
                "reach_score": round(float(r[8] or 0), 1),
                "slug": r[9],
                "category": _categorize_post(r[3] or []),
            }
            for r in raw_rows
        ]
    except Exception:
        all_posts = []

    # Group into category sections when no category filter is active
    if category:
        sections = [{"key": category, "label": _CATEGORIES[category]["label"], "posts": all_posts}]
    else:
        seen_ids: set = set()
        sections = []
        for key, cat in _CATEGORIES.items():
            cat_posts = [p for p in all_posts if p["category"] == key and p["id"] not in seen_ids][:5]
            for p in cat_posts:
                seen_ids.add(p["id"])
            if cat_posts:
                sections.append({"key": key, "label": cat["label"], "posts": cat_posts})
        # Catch-all for uncategorized posts
        other = [p for p in all_posts if p["id"] not in seen_ids][:5]
        if other:
            sections.append({"key": "other", "label": "Ideas & More", "posts": other})

    # Authenticated user's draft posts
    if current_user.is_authenticated:
        try:
            cfg = get_config()
            with psycopg.connect(cfg.database_url) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT sp.id, sp.title, sp.topic_tags, sp.format, sp.created_at
                        FROM social_posts sp
                        JOIN users u ON u.id = sp.author_user_id
                        WHERE u.username = %s AND sp.status = 'draft'
                        ORDER BY sp.created_at DESC LIMIT 3;
                        """,
                        (current_user.username,),
                    )
                    for r in cur.fetchall():
                        draft_posts.append({
                            "id": str(r[0]), "title": r[1],
                            "topic_tags": r[2] or [], "format": r[3],
                            "created_at": _ago(r[4]),
                        })
        except Exception:
            pass

    return render_template("site/landing.html",
                           sections=sections,
                           draft_posts=draft_posts,
                           sort=sort,
                           category=category,
                           categories=_CATEGORIES,
                           last_seen_at=last_seen_at,
                           user_atom_count=user_atom_count,
                           valid_sorts=("for_you", "new", "popular", "rising"))


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
    from flask import request as _req  # noqa: PLC0415

    sort       = _req.args.get("sort", "newest")
    type_filter = _req.args.get("type", "")
    vis_filter  = _req.args.get("vis", "")
    contested_only = _req.args.get("contested", "") == "1"

    _sort_map = {
        "newest":     "ma.created_at DESC",
        "oldest":     "ma.created_at ASC",
        "importance": "ma.importance DESC, ma.created_at DESC",
        "confidence": "ma.confidence DESC, ma.created_at DESC",
    }
    order_clause = _sort_map.get(sort, "ma.created_at DESC")

    _valid_types = {
        "opinion", "preference", "belief", "decision",
        "fact", "observation", "correction", "instruction",
    }
    _valid_vis = {"public", "private"}

    where_extras = []
    params: list = [current_user.username]
    if type_filter in _valid_types:
        where_extras.append("AND ma.memory_type = %s")
        params.append(type_filter)
    if vis_filter in _valid_vis:
        where_extras.append("AND ma.visibility = %s")
        params.append(vis_filter)
    if contested_only:
        where_extras.append("AND ma.disagreement_score >= 0.5")

    extra_sql = " ".join(where_extras)

    atoms = []
    stats = {"total": 0, "facts": 0, "decisions": 0, "contested": 0}
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT DISTINCT ma.id, ma.content, ma.memory_type, ma.scope,
                           ma.confidence, ma.importance, ma.lifecycle_status, ma.created_at,
                           ma.disagreement_score, ma.visibility
                    FROM memory_atoms ma
                    JOIN memory_signals ms ON ms.memory_atom_id = ma.id
                    WHERE (ms.source_user_id = %s OR ms.source_user_id IS NULL)
                      AND ma.lifecycle_status = 'active'
                      {extra_sql}
                    ORDER BY {order_clause}
                    LIMIT 100;
                    """,
                    params,
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
                        "created_at": r[7].strftime("%Y-%m-%d %H:%M") if r[7] else "—",
                        "contested": float(r[8] or 0) >= 0.5,
                        "visibility": r[9] or "private",
                    }
                    for r in rows
                ]
                cur.execute(
                    """
                    SELECT
                        COUNT(DISTINCT ma.id),
                        COUNT(DISTINCT ma.id) FILTER (WHERE ma.memory_type = 'fact'),
                        COUNT(DISTINCT ma.id) FILTER (WHERE ma.memory_type = 'decision'),
                        COUNT(DISTINCT ma.id) FILTER (WHERE ma.disagreement_score >= 0.5)
                    FROM memory_atoms ma
                    JOIN memory_signals ms ON ms.memory_atom_id = ma.id
                    WHERE (ms.source_user_id = %s OR ms.source_user_id IS NULL)
                      AND ma.lifecycle_status = 'active';
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
    return render_template(
        "site/brain.html",
        atoms=atoms,
        stats=stats,
        sort=sort,
        type_filter=type_filter,
        vis_filter=vis_filter,
        contested_only=contested_only,
    )


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
            f"Captured {'(kept private)' if make_private else '— public and will appear in the feed'}.",
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


@site_bp.route("/post/<post_ref>")
def post_detail(post_ref):
    import uuid as _uuid  # noqa: PLC0415
    # Accept UUID or slug. UUID hits 301-redirect to the canonical slug URL.
    try:
        _uuid.UUID(post_ref)
        lookup_col = "id"
    except ValueError:
        lookup_col = "slug"

    post_id = post_ref  # used below as the query parameter
    post = None
    user_vote = None
    ai_responses = []
    has_pending_perspective = False
    pending_perspective_body = None
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT sp.id, sp.title, sp.body, sp.format, sp.topic_tags,
                           sp.confidence_at_publish, sp.published_at, sp.status,
                           u.username AS author, sp.perspective_count,
                           sp.consensus_body, sp.consensus_updated_at, sp.slug
                    FROM social_posts sp
                    LEFT JOIN users u ON u.id = sp.author_user_id
                    WHERE sp.{lookup_col} = %s;
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
                        "perspective_count": r[9] or 0,
                        "consensus_body": r[10],
                        "consensus_updated_at": r[11].strftime("%Y-%m-%d %H:%M") if r[11] else None,
                        "slug": r[12],
                    }
                # Fetch current user's reaction (for button active state)
                user_vote = None
                if current_user.is_authenticated:
                    cur.execute(
                        """
                        SELECT pr.vote FROM post_reactions pr
                        JOIN users u ON u.id = pr.user_id
                        WHERE pr.post_id = %s AND u.username = %s;
                        """,
                        (str(post_id), current_user.username),
                    )
                    vote_row = cur.fetchone()
                    user_vote = vote_row[0] if vote_row else None

                # Fetch AI-generated responses ordered by engagement
                cur.execute(
                    """
                    SELECT r.id, r.body, r.reach_score,
                           COALESCE(rr.vote, NULL) AS user_vote,
                           r.response_kind
                    FROM post_ai_responses r
                    LEFT JOIN users u2 ON u2.username = %s
                    LEFT JOIN post_response_reactions rr
                           ON rr.response_id = r.id AND rr.user_id = u2.id
                    WHERE r.post_id = %s
                    ORDER BY r.response_kind = 'synthesis' DESC,
                             r.reach_score DESC, r.created_at ASC;
                    """,
                    (current_user.username if current_user.is_authenticated else None,
                     str(post_id)),
                )
                ai_responses = [
                    {
                        "id": str(r[0]),
                        "body": r[1],
                        "reach_score": float(r[2]),
                        "user_vote": r[3],
                        "kind": r[4] or "synthesis",
                    }
                    for r in cur.fetchall()
                ]

                # Check if the current user has a perspective still being processed
                has_pending_perspective = False
                pending_perspective_body = None
                if current_user.is_authenticated:
                    cur.execute(
                        """
                        SELECT body FROM perspectives
                        WHERE post_id = %s
                          AND author_user_id = (SELECT id FROM users WHERE username = %s)
                          AND atom_id IS NULL
                        ORDER BY created_at DESC
                        LIMIT 1;
                        """,
                        (str(post_id), current_user.username),
                    )
                    row = cur.fetchone()
                    if row:
                        has_pending_perspective = True
                        pending_perspective_body = row[0]

    except Exception:
        pass
    if not post:
        return "Post not found", 404

    # 301 redirect UUID access to canonical slug URL
    if lookup_col == "id" and post.get("slug"):
        return redirect(url_for("webapp.post_detail", post_ref=post["slug"]), 301)

    # Record view + update interest vector (non-blocking, best-effort)
    if current_user.is_authenticated and post.get("status") == "published":
        try:
            from app.profile_updater import record_post_view  # noqa: PLC0415
            record_post_view(current_user.username, str(post_id))
        except Exception:
            pass

    return render_template(
        "site/post_detail.html",
        post=post,
        user_vote=user_vote,
        ai_responses=ai_responses,
        has_pending_perspective=has_pending_perspective,
        pending_perspective_body=pending_perspective_body,
    )


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
    return redirect(url_for("webapp.post_detail", post_ref=str(post_id)))


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


@site_bp.route("/post/<uuid:post_id>/perspective", methods=["POST"])
@login_required
def post_perspective(post_id):
    """Add a perspective to a post.

    Writes directly to the DB and returns immediately — no LLM in the request
    path. The post_worker background thread picks up perspectives with
    atom_id IS NULL and runs the commit pipeline asynchronously.

    The unique index on (post_id, author_user_id, md5(body)) makes ON CONFLICT
    DO NOTHING the dedup guard — double-submits silently no-op.
    """
    body = (request.form.get("perspective") or "").strip()
    if not body or len(body) < 10:
        flash("Perspective is too short.", "error")
        return redirect(url_for("webapp.post_detail", post_ref=str(post_id)))

    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                # atom_id left NULL — background worker fills it in
                cur.execute(
                    """
                    INSERT INTO perspectives (post_id, author_user_id, author_username, body)
                    VALUES (
                        %s,
                        (SELECT id FROM users WHERE username = %s),
                        %s,
                        %s
                    )
                    ON CONFLICT DO NOTHING;
                    """,
                    (str(post_id), current_user.username, current_user.username, body),
                )
                inserted = cur.rowcount
                if inserted:
                    cur.execute(
                        """
                        UPDATE social_posts
                        SET perspective_count = perspective_count + 1,
                            last_activity_at  = now()
                        WHERE id = %s;
                        """,
                        (str(post_id),),
                    )
                    cur.execute(
                        """
                        INSERT INTO user_notifications (user_id, post_id, new_atom_count, notification_type)
                        SELECT sp.author_user_id, sp.id, 1, 'new_perspective'
                        FROM social_posts sp
                        WHERE sp.id = %s
                          AND sp.author_user_id IS NOT NULL
                          AND sp.author_user_id != (SELECT id FROM users WHERE username = %s)
                        ON CONFLICT (user_id, post_id) WHERE read = false
                        DO UPDATE SET new_atom_count = user_notifications.new_atom_count + 1;
                        """,
                        (str(post_id), current_user.username),
                    )
            conn.commit()
        if inserted:
            flash("Got it — your response is being processed and will appear in the community viewpoints shortly.", "success")
        else:
            flash("You already submitted that response.", "info")
    except Exception as exc:
        _logger.warning("post_perspective: db error: %s", exc)
        flash("Something went wrong. Please try again.", "error")

    return redirect(url_for("webapp.post_detail", post_ref=str(post_id)))


@site_bp.route("/post/<uuid:post_id>/react", methods=["POST"])
@login_required
def post_react(post_id):
    """Record or toggle a thumbs-up / thumbs-down reaction on a post.

    Counts are never displayed. The vote feeds reach_score with lower
    weight than a perspective: up=+0.3, down=-0.1.
    Submitting the same vote again removes it (toggle off).
    """
    vote = request.form.get("vote", "").strip()
    if vote not in ("up", "down"):
        return redirect(url_for("webapp.post_detail", post_ref=str(post_id)))

    WEIGHTS = {"up": 0.3, "down": -0.1}

    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                user_id_row = cur.execute(
                    "SELECT id FROM users WHERE username = %s;",
                    (current_user.username,),
                ).fetchone()
                if not user_id_row:
                    return redirect(url_for("webapp.post_detail", post_ref=str(post_id)))
                user_id = user_id_row[0]

                # Check existing vote
                cur.execute(
                    "SELECT vote FROM post_reactions WHERE post_id = %s AND user_id = %s;",
                    (str(post_id), user_id),
                )
                existing = cur.fetchone()

                if existing and existing[0] == vote:
                    # Same vote — toggle off
                    cur.execute(
                        "DELETE FROM post_reactions WHERE post_id = %s AND user_id = %s;",
                        (str(post_id), user_id),
                    )
                    cur.execute(
                        "UPDATE social_posts SET reach_score = reach_score - %s WHERE id = %s;",
                        (WEIGHTS[vote], str(post_id)),
                    )
                elif existing:
                    # Different vote — swap
                    old_weight = WEIGHTS[existing[0]]
                    new_weight = WEIGHTS[vote]
                    cur.execute(
                        "UPDATE post_reactions SET vote = %s, created_at = now() "
                        "WHERE post_id = %s AND user_id = %s;",
                        (vote, str(post_id), user_id),
                    )
                    cur.execute(
                        "UPDATE social_posts SET reach_score = reach_score - %s + %s WHERE id = %s;",
                        (old_weight, new_weight, str(post_id)),
                    )
                else:
                    # New vote
                    cur.execute(
                        "INSERT INTO post_reactions (post_id, user_id, vote) VALUES (%s, %s, %s);",
                        (str(post_id), user_id, vote),
                    )
                    cur.execute(
                        "UPDATE social_posts SET reach_score = reach_score + %s WHERE id = %s;",
                        (WEIGHTS[vote], str(post_id)),
                    )
            conn.commit()
    except Exception as exc:
        _logger.warning("post_react: error: %s", exc)

    # Update interest vector — reactions are a strong signal
    try:
        from app.profile_updater import record_post_reaction  # noqa: PLC0415
        record_post_reaction(current_user.username, str(post_id), vote)
    except Exception:
        pass

    return redirect(url_for("webapp.post_detail", post_ref=str(post_id)))


@site_bp.route("/post/<uuid:post_id>/open-in-claude")
@login_required
def open_in_claude(post_id):
    """Build a claude:// deep link pre-filled with post context and redirect to it."""
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT title, body, consensus_body FROM social_posts WHERE id = %s;",
                    (str(post_id),),
                )
                row = cur.fetchone()
                if not row:
                    return "Post not found", 404
                title, body, consensus_body = row

                cur.execute(
                    """
                    SELECT COALESCE(author_username, 'anonymous'), body
                    FROM perspectives
                    WHERE post_id = %s
                    ORDER BY created_at ASC;
                    """,
                    (str(post_id),),
                )
                persp_rows = cur.fetchall()
    except Exception as exc:
        _logger.warning("open_in_claude: db error: %s", exc)
        return redirect(url_for("webapp.post_detail", post_ref=str(post_id)))

    CLOSING = (
        "\n\n---\nHelp me engage with this discussion. "
        "Summarize the key points of disagreement and suggest what I might add."
    )
    MAX_CHARS = 12_000

    parts = [f"Analyzing this Synapse post:\n\nTitle: {title}\n\n{body}"]
    total = len(parts[0])

    if persp_rows:
        header = "\n\n---\nCommunity perspectives:"
        parts.append(header)
        total += len(header)
        for author, pbody in persp_rows:
            line = f"\n{author}: {pbody}"
            if total + len(line) > MAX_CHARS:
                break
            parts.append(line)
            total += len(line)

    if consensus_body:
        snippet = f"\n\n---\nCommunity consensus:\n{consensus_body}"
        if total + len(snippet) <= MAX_CHARS:
            parts.append(snippet)

    parts.append(CLOSING)
    context = "".join(parts)

    encoded = _urlquote(context, safe="")
    return redirect(f"claude://claude.ai/new?q={encoded}", code=302)


@site_bp.route("/response/<uuid:response_id>/react", methods=["POST"])
@login_required
def response_react(response_id):
    """Thumbs up / down on an individual AI response. Redirects back to the post."""
    vote = request.form.get("vote", "").strip()
    post_id = request.form.get("post_id", "").strip()
    if vote not in ("up", "down"):
        return redirect(url_for("webapp.landing"))

    WEIGHTS = {"up": 0.3, "down": -0.1}

    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM users WHERE username = %s;",
                            (current_user.username,))
                row = cur.fetchone()
                if not row:
                    return redirect(url_for("webapp.post_detail", post_ref=str(post_id)) if post_id else url_for("webapp.landing"))
                user_id = row[0]

                cur.execute(
                    "SELECT vote FROM post_response_reactions WHERE response_id = %s AND user_id = %s;",
                    (str(response_id), user_id),
                )
                existing = cur.fetchone()

                if existing and existing[0] == vote:
                    cur.execute(
                        "DELETE FROM post_response_reactions WHERE response_id = %s AND user_id = %s;",
                        (str(response_id), user_id),
                    )
                    cur.execute(
                        "UPDATE post_ai_responses SET reach_score = reach_score - %s WHERE id = %s;",
                        (WEIGHTS[vote], str(response_id)),
                    )
                elif existing:
                    old_w, new_w = WEIGHTS[existing[0]], WEIGHTS[vote]
                    cur.execute(
                        "UPDATE post_response_reactions SET vote = %s, created_at = now() "
                        "WHERE response_id = %s AND user_id = %s;",
                        (vote, str(response_id), user_id),
                    )
                    cur.execute(
                        "UPDATE post_ai_responses SET reach_score = reach_score - %s + %s WHERE id = %s;",
                        (old_w, new_w, str(response_id)),
                    )
                else:
                    cur.execute(
                        "INSERT INTO post_response_reactions (response_id, user_id, vote) VALUES (%s, %s, %s);",
                        (str(response_id), user_id, vote),
                    )
                    cur.execute(
                        "UPDATE post_ai_responses SET reach_score = reach_score + %s WHERE id = %s;",
                        (WEIGHTS[vote], str(response_id)),
                    )
            conn.commit()
    except Exception as exc:
        _logger.warning("response_react: error: %s", exc)

    if post_id:
        return redirect(url_for("webapp.post_detail", post_ref=str(post_id)))
    return redirect(url_for("webapp.landing"))


@site_bp.route("/drafts/<uuid:post_id>/regenerate", methods=["POST"])
@login_required
def regenerate_draft(post_id):
    """Rewrite a draft in place, consuming 1 regen token."""
    from app.article_generator import generate_draft  # noqa: PLC0415

    notes = request.form.get("notes", "").strip()

    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT regen_tokens FROM users WHERE username = %s;",
                    (current_user.username,),
                )
                token_row = cur.fetchone()
                if not token_row or int(token_row[0]) < 1:
                    flash("No rewrites left today — tokens refresh every 24 hours.", "error")
                    return redirect(url_for("webapp.drafts"))

                cur.execute(
                    """
                    SELECT sp.primary_atom_ids, sp.format
                    FROM social_posts sp
                    JOIN users u ON u.id = sp.author_user_id
                    WHERE sp.id = %s AND u.username = %s AND sp.status = 'draft';
                    """,
                    (str(post_id), current_user.username),
                )
                row = cur.fetchone()
        if not row:
            flash("Draft not found.", "error")
            return redirect(url_for("webapp.drafts"))

        atom_ids = [str(a) for a in (row[0] or [])]
        fmt = row[1] or "article"

        result = generate_draft(
            atom_ids=atom_ids,
            author_username=current_user.username,
            format_override=fmt,
            extra_context=notes or None,
            update_post_id=str(post_id),
        )
        if not result:
            flash("Could not regenerate — try again shortly.", "error")
            return redirect(url_for("webapp.drafts"))

        # Deduct token only after a successful rewrite
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE users
                    SET regen_tokens = GREATEST(0, regen_tokens - 1)
                    WHERE username = %s;
                    """,
                    (current_user.username,),
                )
            conn.commit()
        flash("Draft rewritten.", "success")
    except Exception:
        flash("Regeneration failed — try again.", "error")

    return redirect(url_for("webapp.drafts"))


@site_bp.route("/drafts")
@login_required
def drafts():
    posts = []
    regen_tokens = 5
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT regen_tokens FROM users WHERE username = %s;",
                    (current_user.username,),
                )
                token_row = cur.fetchone()
                if token_row:
                    regen_tokens = int(token_row[0])

                cur.execute(
                    """
                    SELECT sp.id, sp.title, sp.format, sp.confidence_at_publish,
                           sp.created_at, sp.body, sp.source_turn_text,
                           sp.topic_tags, sp.slug
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
                        "created_at": _ago(r[4]) if r[4] else "—",
                        "preview": (r[5] or "")[:200],
                        "source_hint": (r[6] or "")[:120] if r[6] else None,
                        "topic_tags": r[7] or [],
                        "slug": r[8],
                    }
                    for r in rows
                ]
    except Exception:
        pass

    return render_template("site/drafts.html", posts=posts, regen_tokens=regen_tokens)


@site_bp.route("/drafts/generate", methods=["POST"])
@login_required
def generate_draft_from_suggestion():
    """Turn a headline suggestion into a full draft by calling the article generator."""
    import json as _json
    from app.article_generator import generate_draft

    raw_ids = request.form.get("atom_ids", "")
    format_hint = request.form.get("format", None)
    try:
        atom_ids = _json.loads(raw_ids) if raw_ids.startswith("[") else raw_ids.split(",")
        atom_ids = [a.strip() for a in atom_ids if a.strip()]
    except Exception:
        atom_ids = []

    if not atom_ids:
        flash("No atoms selected — nothing to generate.", "error")
        return redirect(url_for("webapp.drafts"))

    result = generate_draft(
        atom_ids=atom_ids,
        author_username=current_user.username,
        format_override=format_hint or None,
    )
    if result:
        flash("Draft created — review it below.", "success")
    else:
        flash("Could not generate a draft right now. Try again shortly.", "error")
    return redirect(url_for("webapp.drafts"))


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

                cur.execute(
                    """
                    SELECT
                        COUNT(*) FILTER (WHERE status = 'published') AS published,
                        COUNT(*) FILTER (WHERE status = 'draft') AS draft,
                        COALESCE(SUM(perspective_count), 0) AS perspectives
                    FROM social_posts;
                    """
                )
                res = cur.fetchone()
                stats["posts_published"] = res[0] or 0
                stats["posts_draft"] = res[1] or 0
                stats["perspectives"] = res[2] or 0
                stats["discussions"] = stats["posts_published"]  # legacy key

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
                    SELECT id, title, topic_tags, perspective_count,
                           perspective_count, false, last_activity_at
                    FROM social_posts WHERE status = 'published'
                    ORDER BY perspective_count DESC LIMIT 200;
                """)
                rows = cur.fetchall()

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

    import logging as _logging
    _mcp_log = _logging.getLogger("mcp_sse")
    _mcp_log.info("mcp_sse tool=%s user=%s", tool, username)

    try:
        result = _dispatch_mcp_tool(tool, args, username)
        _mcp_log.info("mcp_sse tool=%s user=%s status=ok", tool, username)
        return jsonify({"result": result})
    except ValueError as exc:
        _mcp_log.warning("mcp_sse tool=%s user=%s status=error err=%s", tool, username, exc)
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        _mcp_log.warning("mcp_sse tool=%s user=%s status=error err=%s", tool, username, exc)
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

    if tool == "memory_push_conversation":
        from mcp_server.tools.push_conversation import push_conversation_tool
        return push_conversation_tool(
            transcript=args.get("transcript", ""),
            source_user_id=username,
            is_jsonl_path=bool(args.get("is_jsonl_path", False)),
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

    # ── Deterministic guardrail (credentials / secrets) ──
    from app.write_quality import score_write_quality
    quality = score_write_quality(content, memory_type=memory_type, scope=scope)
    if quality.decision == "reject":
        return jsonify({
            "stored": False,
            "decision": "rejected",
            "reason": "; ".join(quality.signals),
        }), 400

    # ── Commit pipeline ──
    try:
        from app.commit_pipeline import MemoryCommitPipeline
        pipeline = MemoryCommitPipeline()
        decision = pipeline.commit_candidate(
            {
                "content": content,
                "memory_type": memory_type,
                "scope": scope,
                "should_store": True,
            },
            source_key=f"{source_label}:{username}",
            source_type="api",
            source_user_id=username,
            visibility=visibility,
        )
        stored = decision.committed_atom_id is not None
        return jsonify({
            "stored": stored,
            "decision": decision.decision,
            "atom_id": decision.committed_atom_id,
            "signal_id": decision.committed_signal_id,
            "confidence": decision.confidence,
            "content": content,
        }), 200 if stored else 202
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ── Discussion routes ─────────────────────────────────────────────────────────

@site_bp.route("/explore")
@login_required
def explore():
    """Published posts ranked by topic affinity; recency fallback for new users."""
    rows = []
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT sp.id, sp.title, sp.topic_tags, sp.perspective_count,
                           sp.last_activity_at, LEFT(sp.body, 300) AS summary,
                           u.username AS author, sp.slug
                    FROM social_posts sp
                    LEFT JOIN users u ON u.id = sp.author_user_id
                    WHERE sp.status = 'published'
                    ORDER BY sp.last_activity_at DESC
                    LIMIT 100;
                    """
                )
                for r in cur.fetchall():
                    rows.append({
                        "id": str(r[0]),
                        "title": r[1],
                        "topic_tags": r[2] or [],
                        "perspective_count": r[3] or 0,
                        "last_activity": _ago(r[4]),
                        "summary": r[5] or "",
                        "author": r[6] or "anonymous",
                        "slug": r[7],
                    })
    except Exception:
        pass

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
            pass

    return render_template("site/explore.html", discussions=rows, user_tags=user_tags)


@site_bp.route("/categories")
@login_required
def categories():
    """Browse published posts by topic tag."""
    selected = request.args.get("tag", "").strip().lower()
    all_tags: list[str] = []
    tag_counts: dict[str, int] = {}
    discussions: list[dict] = []

    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT unnest(topic_tags) AS tag, COUNT(*) AS cnt
                    FROM social_posts
                    WHERE status = 'published'
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
                        SELECT sp.id, sp.title, sp.topic_tags, sp.perspective_count,
                               sp.last_activity_at, LEFT(sp.body, 300),
                               u.username, sp.slug
                        FROM social_posts sp
                        LEFT JOIN users u ON u.id = sp.author_user_id
                        WHERE sp.status = 'published'
                          AND %s = ANY(sp.topic_tags)
                        ORDER BY sp.last_activity_at DESC
                        LIMIT 50;
                        """,
                        (selected,),
                    )
                    for r in cur.fetchall():
                        discussions.append({
                            "id": str(r[0]),
                            "title": r[1],
                            "topic_tags": r[2] or [],
                            "perspective_count": r[3] or 0,
                            "last_activity": _ago(r[4]),
                            "summary": r[5] or "",
                            "author": r[6] or "anonymous",
                            "slug": r[7],
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
    """Search published posts and memory atoms."""
    query = request.args.get("q", "").strip()
    results: list[dict] = []
    atom_results: list[dict] = []

    if query:
        try:
            with _conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT sp.id, sp.title, sp.topic_tags, sp.perspective_count,
                               sp.last_activity_at, LEFT(sp.body, 300), u.username, sp.slug
                        FROM social_posts sp
                        LEFT JOIN users u ON u.id = sp.author_user_id
                        WHERE sp.status = 'published'
                          AND (sp.title ILIKE %s OR sp.body ILIKE %s
                               OR %s = ANY(sp.topic_tags))
                        ORDER BY sp.perspective_count DESC, sp.last_activity_at DESC
                        LIMIT 20;
                        """,
                        (f"%{query}%", f"%{query}%", query.lower()),
                    )
                    for r in cur.fetchall():
                        results.append({
                            "id": str(r[0]),
                            "title": r[1],
                            "topic_tags": r[2] or [],
                            "perspective_count": r[3] or 0,
                            "last_activity": _ago(r[4]),
                            "summary": r[5] or "",
                            "author": r[6] or "anonymous",
                            "slug": r[7],
                        })
        except Exception:
            pass

        try:
            from app.topic_affinity import get_user_topic_tags  # noqa: F401
            import os as _os
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
def discussions_redirect():
    return redirect(url_for("webapp.feed"), 301)


@site_bp.route("/discussion/<uuid:disc_id>")
def discussion_detail_redirect(disc_id):
    return redirect(url_for("webapp.feed"), 301)


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
                    SELECT n.id, sp.id, sp.title, n.new_atom_count,
                           n.read, n.created_at, n.notification_type, sp.slug
                    FROM user_notifications n
                    JOIN social_posts sp ON sp.id = n.post_id
                    WHERE n.user_id = (SELECT id FROM users WHERE username = %s)
                    ORDER BY n.created_at DESC
                    LIMIT 50;
                    """,
                    (current_user.username,),
                )
                for r in cur.fetchall():
                    notif_type = r[6] or "new_perspective"
                    count = r[3]
                    if notif_type == "new_perspective":
                        message = f"{count} new perspective{'s' if count != 1 else ''} on your post."
                    else:
                        message = f"Activity on your post ({notif_type})."
                    items.append({
                        "id": str(r[0]),
                        "post_id": str(r[1]),
                        "post_slug": r[7] or str(r[1]),
                        "post_title": r[2],
                        "message": message,
                        "read": r[4],
                        "created_at": _ago(r[5]),
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

    # Pre-load post context when arriving via "Chat about this" link
    if request.method == "GET" and not history:
        disc_param = request.args.get("post", "").strip()
        if disc_param:
            try:
                with _conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT title, perspective_count FROM social_posts WHERE id = %s;",
                            (disc_param,),
                        )
                        disc_row = cur.fetchone()
                if disc_row:
                    disc_title, contributor_count = disc_row
                    context_msg = (
                        f"I'd like to discuss: **{disc_title}**"
                        + f"\n\nThis post has {contributor_count} perspective(s) on it. "
                        "What can you tell me about this topic and what should I know?"
                    )
                    history = [{"role": "user", "content": context_msg}]
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

                from app.chat import _is_routeable_question
                raw_answer, memories, research_results, research_status, context_eval, gap_state, route = chat_with_research(
                    messages_for_llm,
                    source_user_id=current_user.username,
                )
                answer = clean_assistant_response(raw_answer)
                was_routed = route == "direct" and _is_routeable_question(message)

                history.append({"role": "user", "content": message})
                history.append({
                    "role": "assistant",
                    "content": answer,
                    "was_routed": was_routed,
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


@site_bp.route("/privacy")
def privacy():
    return render_template("site/privacy.html")


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
