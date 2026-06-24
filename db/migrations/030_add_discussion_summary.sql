-- Migration 030: add summary column to discussions.
-- Referenced by explore, discussions, search, and chat routes but missing
-- from the initial schema — caused ProgrammingError on every page load.
-- Populated by discussion_synthesizer when it commits a synthesis atom.
ALTER TABLE discussions
    ADD COLUMN IF NOT EXISTS summary TEXT;
