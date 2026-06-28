"""Generate draft social posts from clusters of public memory atoms.

Format is auto-detected from atom metadata and signal patterns.
Output is written to social_posts as status='draft' for user review.
"""
from __future__ import annotations

import json
import uuid
from typing import Any

import psycopg

from app.config import get_config
from app.llm_provider import get_llm_client

_FORMAT_SYSTEM = (
    "You are a writer for a personal knowledge publishing platform. "
    "Your job is to turn a person's genuine ideas, experiences, and opinions into posts worth reading.\n\n"
    "REJECT (return {\"title\":null,\"body\":null,\"topic_tags\":[]}) ONLY if the content is:\n"
    "- Pure credentials, secrets, or private personal data\n"
    "- Completely incoherent — no idea can be extracted\n"
    "- Purely ephemeral logistics with zero outside-reader value (e.g. 'meeting at 3pm')\n\n"
    "ACCEPT everything else and write energetically — including opinions about software, "
    "AI tools, platforms, or technical experiences. A person's genuine perspective on "
    "building with AI, using a platform, or their relationship with technology is as "
    "publishable as any other domain. The test is: does this contain a real idea, "
    "experience, or opinion that an outside reader could engage with? If yes, write it.\n\n"
    "READING THE ATOM METADATA — use conf and imp as expression signals, not filters:\n"
    "- conf (confidence 0–1): how certain the contributor is.\n"
    "  >0.8 → conviction and declarative voice.\n"
    "  0.5–0.8 → hedge: 'I think', 'it seems', 'probably'.\n"
    "  <0.5 → open question or live hypothesis still being worked out.\n"
    "- imp (importance 0–1): how much this matters to the contributor.\n"
    "  >0.7 → core idea — lead with it, give it a full section.\n"
    "  <0.4 → supporting colour — mention briefly or fold into another point.\n"
    "When atoms conflict on the same topic, surface the tension. Don't smooth it over.\n\n"
    "ATTRIBUTION — when atoms carry 'by:@username' and imp >0.7, credit the contributor "
    "naturally in the prose: 'as @alice observed', '@bob\\'s key point here', "
    "'building on @carol\\'s experience...'. Weave it in — never list usernames as a footer. "
    "For imp ≤0.4 atoms, attribution is optional.\n\n"
    "When writing: match the energy of the source conversation — playful means witty, "
    "analytical means sharp, enthusiastic means it shows. "
    "Never write in dry academic or neutral press-release voice. "
    "Never mention the AI system. Write in first or third person as fits the format.\n\n"
    "FORMATTING — always use markdown structure in the body:\n"
    "- Open with a strong hook paragraph (no heading needed)\n"
    "- Use ## headings to break the piece into 2–4 sections\n"
    "- Use **bold** for key terms or emphasis\n"
    "- Use bullet lists or numbered lists where they aid clarity\n"
    "- Close with an engagement hook that matches the tone: for personal or emotional content, "
    "a reflective question or quiet invitation ('Has this changed how you think about X?'); "
    "for analytical content, a sharp challenge or open question; "
    "for playful content, wit. Never use promotional language ('Let me know!', 'Share this!'). "
    "The closing should feel like the natural end of a conversation, not a call-to-action.\n"
    "Aim for 300–600 words. Not a wall of text, not a list of bullets — a real piece.\n\n"
    "Return ONLY a JSON object: {\"title\": string|null, \"body\": string|null, "
    "\"topic_tags\": array}. No markdown fences around the JSON itself."
)

_FORMAT_PROMPTS: dict[str, str] = {
    "tutorial": (
        "Write a how-to guide. Open with why this matters, then use **numbered steps** "
        "with clear headers for each major phase. Add a tips or gotchas section at the end."
    ),
    "discussion": (
        "Write a discussion piece exploring this topic from multiple angles. "
        "Use ## headings for each perspective. Acknowledge disagreements directly — "
        "don't smooth them over. End with your current best read."
    ),
    "open_question": (
        "Write a short post stating the belief, its confidence level, and why you're uncertain. "
        "Use **bold** for the core claim. Invite the reader to push back or add evidence."
    ),
    "news_brief": (
        "Write a punchy news brief. Lead with the most important fact. "
        "Use short paragraphs (2–3 sentences each). Note any conflicting signals with a "
        "**Note:** prefix."
    ),
    "narrative": (
        "Write a first-person account. Make it specific and grounded — real details, "
        "not generalities. Use scene-setting before analysis. "
        "A quote or moment from the experience makes it land."
    ),
    "article": (
        "Write an engaging article. Hook the reader in the first two sentences. "
        "Use ## section headings. Mix paragraphs with the occasional bullet list "
        "where it genuinely helps. End with a clear takeaway or next question."
    ),
}


def _detect_format(atoms: list[dict[str, Any]]) -> str:
    """Infer the best post format from atom types and signal patterns."""
    types = [a.get("memory_type", "") for a in atoms]
    type_counts: dict[str, int] = {}
    for t in types:
        type_counts[t] = type_counts.get(t, 0) + 1

    contested = sum(1 for a in atoms if float(a.get("disagreement_score", 0)) >= 0.4)
    total = len(atoms)

    # High disagreement → discussion or open question
    if contested / max(total, 1) >= 0.4:
        avg_conf = sum(float(a.get("confidence", 0.5)) for a in atoms) / max(total, 1)
        return "open_question" if avg_conf < 0.6 else "discussion"

    # Step-heavy observations → tutorial
    if type_counts.get("observation", 0) >= total * 0.6:
        return "tutorial"

    # Mostly beliefs/decisions → discussion
    belief_types = {"belief", "decision", "opinion", "preference"}
    if sum(type_counts.get(t, 0) for t in belief_types) >= total * 0.5:
        return "discussion"

    # Multiple high-confidence facts from different sources → news brief
    unique_sources = len({a.get("source_user_id") for a in atoms if a.get("source_user_id")})
    if unique_sources >= 2 and type_counts.get("fact", 0) >= 2:
        return "news_brief"

    return "article"


def generate_draft(
    atom_ids: list[str],
    author_username: str,
    format_override: str | None = None,
    extra_context: str | None = None,
    update_post_id: str | None = None,
) -> dict[str, Any] | None:
    """Generate a draft social post from a cluster of atom IDs.

    extra_context: optional user-supplied notes to guide the rewrite.
    update_post_id: if provided, UPDATE that post in place instead of
    inserting a new row. Use this for regen-in-place so no orphan drafts
    are created.
    Returns the post dict, or None on failure.
    """
    if not atom_ids:
        return None

    cfg = get_config()

    # Fetch atoms + context_summary (carries tone tags from the critic)
    atoms: list[dict[str, Any]] = []
    with psycopg.connect(cfg.database_url) as conn:
        with conn.cursor() as cur:
            placeholders = ",".join(["%s"] * len(atom_ids))
            cur.execute(
                f"""
                SELECT ma.id, ma.content, ma.memory_type, ma.scope, ma.confidence,
                       ma.importance, ma.disagreement_score, ma.source_type,
                       ms.source_user_id, ms.context_summary
                FROM memory_atoms ma
                LEFT JOIN LATERAL (
                    SELECT source_user_id, context_summary FROM memory_signals
                    WHERE memory_atom_id = ma.id
                    ORDER BY created_at DESC LIMIT 1
                ) ms ON true
                WHERE ma.id IN ({placeholders})
                  AND ma.lifecycle_status = 'active';
                """,
                [str(a) for a in atom_ids],
            )
            rows = cur.fetchall()
            atoms = [
                {
                    "id": str(r[0]),
                    "content": r[1],
                    "memory_type": r[2],
                    "scope": r[3],
                    "confidence": float(r[4]),
                    "importance": float(r[5]),
                    "disagreement_score": float(r[6] or 0),
                    "source_type": r[7],
                    "source_user_id": r[8],
                    "context_summary": r[9] or "",
                }
                for r in rows
            ]

    if not atoms:
        return None

    # Extract tone tags from context_summaries — format: [tone:X]
    import re as _re
    tone_tags = []
    for a in atoms:
        found = _re.findall(r'\[tone:([^\]]+)\]', a.get("context_summary", ""))
        tone_tags.extend(found)
    dominant_tone = max(set(tone_tags), key=tone_tags.count) if tone_tags else None

    fmt = format_override or _detect_format(atoms)
    prompt_extra = _FORMAT_PROMPTS.get(fmt, _FORMAT_PROMPTS["article"])

    atom_lines = []
    for a in atoms:
        meta = f"[{a['memory_type']} conf={a['confidence']:.2f} imp={a['importance']:.2f}"
        if a.get("source_user_id") and a.get("importance", 0) > 0.4:
            meta += f" by:@{a['source_user_id']}"
        meta += "]"
        atom_lines.append(f"- {meta} {a['content']}")
    atom_summary = "\n".join(atom_lines)
    tone_instruction = (
        f"Write with a {dominant_tone} tone — the original conversation had this energy, "
        "so the article should reflect it rather than flatten it into neutral prose."
        if dominant_tone else
        "Write with personality and genuine interest in the subject — not flat or academic."
    )
    user_notes = (
        f"\nAuthor's additional direction: {extra_context}\n"
        if extra_context else ""
    )
    prompt = (
        f"{prompt_extra}\n\n"
        f"Tone guidance: {tone_instruction}\n"
        f"{user_notes}\n"
        f"Beliefs and observations to synthesize:\n{atom_summary}\n\n"
        "Return JSON only."
    )

    try:
        llm = get_llm_client()
        raw = llm.generate_response(prompt, system=_FORMAT_SYSTEM, json_mode=True)
        parsed = json.loads(raw) if isinstance(raw, str) else raw
        # LLM returns null title/body when content doesn't meet quality bar
        if parsed.get("title") is None or parsed.get("body") is None:
            return None
        title = str(parsed["title"]).strip()
        body = str(parsed["body"]).strip()
        tags = [str(t).strip() for t in parsed.get("topic_tags", []) if t][:5]
    except Exception:
        return None

    if not title or not body:
        return None

    avg_conf = sum(a["confidence"] for a in atoms) / len(atoms)

    if update_post_id:
        # Update existing draft in place — no new row created
        with psycopg.connect(cfg.database_url) as conn:
            from app.utils import unique_slug  # noqa: PLC0415
            slug = unique_slug(conn, title, exclude_id=update_post_id)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE social_posts
                    SET title = %s, body = %s, topic_tags = %s, slug = %s, updated_at = now()
                    WHERE id = %s::uuid AND status = 'draft'
                    RETURNING id, title, format, status, created_at;
                    """,
                    (title, body, tags, slug, update_post_id),
                )
                post_row = cur.fetchone()
            conn.commit()

        if not post_row:
            return None

        post_id = str(post_row[0])
        _store_post_embedding(post_id, atom_ids, cfg)
        return {
            "id": post_id,
            "title": title,
            "format": str(post_row[2]),
            "status": "draft",
            "created_at": str(post_row[4]),
        }

    with psycopg.connect(cfg.database_url) as conn:
        from app.utils import unique_slug  # noqa: PLC0415
        slug = unique_slug(conn, title)

        with conn.cursor() as cur:
            # Resolve author user id
            cur.execute("SELECT id FROM users WHERE username = %s;", (author_username,))
            row = cur.fetchone()
            author_id = str(row[0]) if row else None

            # source_turn_text: first atom's content so /drafts can show
            # "From your conversation: ..." without the user needing to dig in.
            source_hint = atoms[0]["content"][:500] if atoms else None

            cur.execute(
                """
                INSERT INTO social_posts
                    (title, body, format, status, author_user_id,
                     primary_atom_ids, topic_tags, confidence_at_publish,
                     source_turn_text, slug)
                VALUES (%s, %s, %s, 'draft', %s, %s, %s, %s, %s, %s)
                RETURNING id, title, format, status, created_at;
                """,
                (
                    title, body, fmt, author_id,
                    [str(a) for a in atom_ids],
                    tags,
                    round(avg_conf, 3),
                    source_hint,
                    slug,
                ),
            )
            post_row = cur.fetchone()
            post_id = str(post_row[0])

            if author_id:
                cur.execute(
                    """
                    INSERT INTO post_contributors (post_id, user_id, contribution_type, atom_ids)
                    VALUES (%s, %s, 'primary', %s);
                    """,
                    (post_id, author_id, [str(a) for a in atom_ids]),
                )
        conn.commit()

    # Compute and store post embedding from atom centroids, then trigger consensus
    _store_post_embedding(post_id, atom_ids, cfg)

    return {
        "id": post_id,
        "title": title,
        "format": fmt,
        "status": "draft",
        "created_at": str(post_row[4]),
    }


def _store_post_embedding(post_id: str, atom_ids: list, cfg: Any) -> None:
    """Compute post embedding as centroid of its atoms and store it, then run consensus."""
    try:
        with psycopg.connect(cfg.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE social_posts
                    SET embedding = (
                        SELECT AVG(embedding)
                        FROM memory_atoms
                        WHERE id = ANY(%s::uuid[]) AND embedding IS NOT NULL
                    )
                    WHERE id = %s;
                    """,
                    ([str(a) for a in atom_ids], post_id),
                )
            conn.commit()

        # Trigger consensus synthesis and multi-response generation
        from app.consensus_synthesizer import update_post_consensus  # noqa: PLC0415
        from app.response_generator import generate_post_responses  # noqa: PLC0415
        update_post_consensus(post_id)
        generate_post_responses(post_id)
    except Exception as exc:
        import logging as _logging  # noqa: PLC0415
        _logging.getLogger(__name__).debug("_store_post_embedding: %s", exc)


def _has_recent_draft_for_tags(
    author_username: str | None,
    topic_tags: list[str] | None,
    within_days: int = 7,
) -> bool:
    """Return True if the user already has a recent draft or published post covering these tags.

    Prevents N drafts accumulating on the same topic when multiple related atoms
    are committed in quick succession. A 7-day window balances freshness vs spam.
    """
    if not author_username or not topic_tags:
        return False

    cfg = get_config()
    try:
        with psycopg.connect(cfg.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COUNT(*)
                    FROM social_posts sp
                    JOIN users u ON u.id = sp.author_user_id
                    WHERE u.username = %s
                      AND sp.status IN ('draft', 'published')
                      AND sp.topic_tags && %s
                      AND sp.created_at >= now() - interval '1 day' * %s;
                    """,
                    (author_username, topic_tags, within_days),
                )
                row = cur.fetchone()
                return bool(row and int(row[0]) > 0)
    except Exception:
        return False
