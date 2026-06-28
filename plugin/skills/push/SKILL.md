---
description: Push a conversation into Synapse memory and generate post drafts. Use when the user says "push this conversation", "save this session to memory", "push to Synapse", or wants to extract memory atoms from a past or current session.
---

# Push Conversation to Synapse

Extract memory atoms and queue post drafts from a conversation session.

## If $ARGUMENTS contains a file path:
Call `memory_push_conversation` with:
- `transcript` = the path from $ARGUMENTS
- `is_jsonl_path` = true

## If $ARGUMENTS is empty or says "this session":
1. Tell the user their current session JSONL is at:
   `~/.claude/projects/<project-slug>/<session-id>.jsonl`
   where session-id is available as `$CLAUDE_SESSION_ID` and project-slug is the
   current directory path with slashes replaced by hyphens.
2. Offer to push the current session by constructing the path and calling
   `memory_push_conversation` with `is_jsonl_path` = true.

## If $ARGUMENTS is raw conversation text:
Call `memory_push_conversation` with:
- `transcript` = the text from $ARGUMENTS
- `is_jsonl_path` = false

## After the call:
Report how many atoms were committed. Tell the user that generated post drafts
are now available at /drafts on their Synapse site. If the result reports 0 atoms,
explain that the conversation may have been mostly small talk or already captured.
