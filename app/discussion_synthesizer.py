"""AI synthesis of discussion reactions — the core of Milestone 2.

When reactions accumulate on a discussion, this module:
  1. Fetches all reaction atoms linked to the discussion
  2. Asks the LLM to generate a single-paragraph synthesis
     ("Based on N perspectives: many think X, some note Y...")
  3. Commits the synthesis through the full write pipeline as a refined atom
  4. Links the synthesis atom back to the discussion
  5. Advances thread_status → 'answered'
  6. Updates the discussion's last_activity_at

The originating user receives the synthesis atom via normal cosine-similarity
retrieval the next time they chat about the same topic. They see a richer
answer — not a thread, not a list of replies. The pipe is fully invisible.
"""
from __future__ import annotations

import logging
import os

_logger = logging.getLogger(__name__)

_MIN_REACTIONS = 1  # synthesise after every reaction for responsiveness
_MAX_ATOMS_FOR_SYNTHESIS = 20


def synthesise_discussion(disc_id: str, db_url: str | None = None) -> str | None:
    """Generate an AI synthesis of all reaction atoms on a discussion.

    Returns the committed synthesis atom_id on success, None on failure.
    Non-fatal — caller should swallow exceptions from this function.
    """
    if not db_url:
        db_url = os.environ.get("DATABASE_URL", "")
    if not db_url:
        return None

    try:
        import psycopg
        with psycopg.connect(db_url) as conn:
            with conn.cursor() as cur:
                # Fetch discussion metadata including age
                cur.execute(
                    """
                    SELECT title, thread_status, created_by_user_id,
                           EXTRACT(EPOCH FROM (now() - created_at)) / 3600 AS age_hours,
                           topic_tags
                    FROM discussions WHERE id = %s;
                    """,
                    (disc_id,),
                )
                disc_row = cur.fetchone()
                if not disc_row:
                    return None
                disc_title, current_status, creator_user_id, age_hours, disc_topic_tags = disc_row

                # Don't re-synthesise if already answered/validated
                if current_status in ("answered", "validated"):
                    return None

                # Fetch reaction atoms with confidence and disagreement for weighting
                cur.execute(
                    """
                    SELECT ma.content, ma.memory_type, ma.confidence,
                           ma.disagreement_score, ma.support_weight
                    FROM discussion_atoms da
                    JOIN memory_atoms ma ON ma.id = da.atom_id
                    WHERE da.discussion_id = %s
                    ORDER BY da.novelty_score DESC,
                             ma.confidence DESC,
                             da.added_at ASC
                    LIMIT %s;
                    """,
                    (disc_id, _MAX_ATOMS_FOR_SYNTHESIS),
                )
                atoms = cur.fetchall()

        if len(atoms) < _MIN_REACTIONS:
            # Mark as unresolved if gathering for > 48 hours with no traction
            if float(age_hours or 0) > 48 and current_status == "gathering":
                try:
                    import psycopg as _pg
                    with _pg.connect(db_url) as conn:
                        with conn.cursor() as cur:
                            cur.execute(
                                """
                                UPDATE discussions
                                SET thread_status = 'unresolved', last_activity_at = now()
                                WHERE id = %s AND thread_status = 'gathering';
                                """,
                                (disc_id,),
                            )
                        conn.commit()
                except Exception:
                    pass
            return None

        # Resolve creator username for scope
        creator_username = None
        if creator_user_id:
            try:
                import psycopg as _pg
                with _pg.connect(db_url) as conn:
                    with conn.cursor() as cur:
                        cur.execute("SELECT username FROM users WHERE id = %s;", (creator_user_id,))
                        row = cur.fetchone()
                        creator_username = row[0] if row else None
            except Exception:
                pass

        synthesis_text = _generate_synthesis(disc_title, atoms)
        if not synthesis_text:
            return None

        # Commit synthesis through the full pipeline.
        # Scope is synapse:validated so ALL users benefit — not just the creator.
        # visibility='public' ensures the atom appears in shared retrieval.
        from app.commit_pipeline import MemoryCommitPipeline
        pipeline = MemoryCommitPipeline()
        candidate = {
            "content": synthesis_text,
            "memory_type": "fact",
            "scope": "synapse:validated",
            "importance": 0.80,
            "should_store": True,
        }
        decision = pipeline.commit_candidate(
            candidate,
            source_key="discussion_synthesis",
            source_type="synthesis",
            source_user_id=creator_username,
        )
        atom_id = decision.committed_atom_id
        if not atom_id:
            return None

        # Force public visibility and propagate topic_tags.
        # The critic may default to private; override so the synthesis propagates.
        try:
            with psycopg.connect(db_url) as _tc:
                with _tc.cursor() as _cur:
                    _cur.execute(
                        """
                        UPDATE memory_atoms
                        SET visibility = 'public',
                            topic_tags  = COALESCE(%s, topic_tags)
                        WHERE id = %s;
                        """,
                        (disc_topic_tags or None, atom_id),
                    )
                _tc.commit()
        except Exception:
            pass  # non-fatal — atom is still valid without these overrides

        # Re-scope discussion-response atoms from discussion:{disc_id} → synapse:validated
        # so their content also feeds future retrievals across all users.
        try:
            with psycopg.connect(db_url) as _tc:
                with _tc.cursor() as _cur:
                    _cur.execute(
                        """
                        UPDATE memory_atoms ma
                        SET scope = 'synapse:validated',
                            visibility = 'public'
                        FROM discussion_atoms da
                        WHERE da.discussion_id = %s
                          AND da.atom_id = ma.id
                          AND ma.scope LIKE 'discussion:%'
                          AND ma.lifecycle_status = 'active';
                        """,
                        (disc_id,),
                    )
                _tc.commit()
        except Exception:
            pass  # non-fatal — individual atom re-scoping

        # Link synthesis atom to discussion.
        # Status progression per spec:
        #   dual-write complete → 'updated' (fire notification)
        #   notification inserted → 'answered' (enriched summary ready)
        with psycopg.connect(db_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO discussion_atoms
                        (discussion_id, atom_id, source_user_id, novelty_score)
                    VALUES (%s, %s, 'synapse_synthesis', 0.8)
                    ON CONFLICT (discussion_id, atom_id) DO NOTHING;
                    """,
                    (disc_id, atom_id),
                )
                # Step 1: mark 'updated' — dual-write is complete
                cur.execute(
                    """
                    UPDATE discussions
                    SET thread_status = 'updated',
                        last_activity_at = now()
                    WHERE id = %s;
                    """,
                    (disc_id,),
                )
                # Step 2: fire 'answered' notification for the originating user
                cur.execute(
                    """
                    INSERT INTO user_notifications
                        (user_id, discussion_id, new_atom_count, notification_type)
                    SELECT d.created_by_user_id, d.id, 1, 'answered'
                    FROM discussions d
                    WHERE d.id = %s
                      AND d.created_by_user_id IS NOT NULL
                    ON CONFLICT (user_id, discussion_id) WHERE read = false
                    DO UPDATE SET new_atom_count = user_notifications.new_atom_count + 1,
                                  notification_type = 'answered';
                    """,
                    (disc_id,),
                )
                # Step 3: advance to 'answered' and store synthesis as summary
                cur.execute(
                    """
                    UPDATE discussions
                    SET thread_status = 'answered',
                        summary = %s
                    WHERE id = %s;
                    """,
                    (synthesis_text[:2000], disc_id),
                )
            conn.commit()

        _logger.info("discussion_synthesizer: synthesis %s committed for discussion %s", atom_id, disc_id)
        return atom_id

    except Exception as exc:
        _logger.warning("discussion_synthesizer: failed for %s: %s", disc_id, exc)
        return None


def _generate_synthesis(title: str, atoms: list[tuple]) -> str | None:
    """Call the LLM to synthesise atom contents into a single paragraph.

    Atoms are labeled by weight tier (high confidence, contested, supporting)
    so the LLM can weight them appropriately in the synthesis.
    Falls back to mechanical concatenation if LLM is unavailable.
    """
    if not atoms:
        return None

    # Label each atom by its confidence + disagreement tier
    labeled: list[str] = []
    for row in atoms:
        content, _mtype, confidence, disagreement, support = row
        if not content:
            continue
        conf = float(confidence or 0.8)
        disc = float(disagreement or 0.0)
        supp = float(support or 0.0)
        if disc > 0.5:
            label = "Contested claim"
        elif conf >= 0.85 and supp >= 1.0:
            label = "High-confidence claim"
        elif supp >= 2.0:
            label = "Widely supported claim"
        else:
            label = "Supporting perspective"
        labeled.append(f"[{label}] {content}")

    n = len(labeled)
    if not labeled:
        return None

    prompt = (
        f"Topic: {title}\n\n"
        f"The following {n} perspective(s) were contributed by different people. "
        "Each is labeled by its confidence weight:\n\n"
        + "\n\n".join(f"{i+1}. {t}" for i, t in enumerate(labeled))
        + "\n\nWrite a single paragraph synthesis starting with "
        f'"Based on {n} perspective{"s" if n != 1 else ""}:". '
        "High-confidence and widely-supported claims should anchor the synthesis. "
        "Contested claims should be presented as open questions. "
        "Under 120 words. No bullet points. Plain prose only."
    )

    try:
        from app.llm_provider import get_chat_client
        llm = get_chat_client()
        result = llm.generate_response(
            prompt,
            system="You synthesise multiple viewpoints into a single clear paragraph. Be concise and neutral.",
        )
        return result.strip() if result else _mechanical_synthesis(labeled)
    except Exception:
        return _mechanical_synthesis(labeled)


def _mechanical_synthesis(labeled: list[str]) -> str:
    """Fallback when LLM is unavailable — joins top 3 labeled perspectives mechanically."""
    n = len(labeled)
    parts = [t[:200] for t in labeled[:3]]
    intro = f"Based on {n} perspective{'s' if n != 1 else ''}: "
    return intro + " Additionally, ".join(parts) + "."
