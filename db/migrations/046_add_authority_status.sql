-- Migration 046: explicit editorial authority for public knowledge consumers.
--
-- Confidence describes how strongly the system believes a claim. Authority
-- describes whether a human/editorial workflow approved the claim for use by a
-- constrained consumer such as a public website. They must not be conflated.

ALTER TABLE memory_atoms
    ADD COLUMN IF NOT EXISTS authority_status TEXT NOT NULL DEFAULT 'unreviewed',
    ADD COLUMN IF NOT EXISTS authority_reviewed_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS authority_reviewer TEXT;

ALTER TABLE memory_atoms
    DROP CONSTRAINT IF EXISTS memory_atoms_authority_status_check;

ALTER TABLE memory_atoms
    ADD CONSTRAINT memory_atoms_authority_status_check
    CHECK (authority_status IN ('unreviewed', 'approved', 'rejected'));

CREATE INDEX IF NOT EXISTS idx_memory_atoms_authority_scope
    ON memory_atoms (scope, authority_status, lifecycle_status);

-- Preserve the intended audience and submitter while evidence waits in the
-- proposal queue. Existing proposals remain private by default.
ALTER TABLE memory_proposals
    ADD COLUMN IF NOT EXISTS visibility TEXT NOT NULL DEFAULT 'private',
    ADD COLUMN IF NOT EXISTS source_user_id TEXT;

ALTER TABLE memory_proposals
    DROP CONSTRAINT IF EXISTS memory_proposals_visibility_check;

ALTER TABLE memory_proposals
    ADD CONSTRAINT memory_proposals_visibility_check
    CHECK (visibility IN ('private', 'team', 'public'));
