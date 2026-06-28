---
description: Search your Synapse memory for past decisions, preferences, and context. Use when the user asks "what did we decide about X", "do you remember Y", references something from a prior conversation, or when relevant past context would help answer a question.
---

# Recall from Synapse Memory

Search Synapse for atoms matching the topic in $ARGUMENTS.

Steps:
1. Call `memory_search` with `query="$ARGUMENTS"` and `limit=8`
2. Present results as a clear list — for each atom show: content, type (decision/preference/fact/etc.), and confidence score
3. Group results by topic if multiple clusters are visible
4. If confidence on any atom is below 0.5, flag it as uncertain

If $ARGUMENTS is empty, ask what the user wants to recall before searching.
If no results are found, say so directly — do not guess or fabricate context.
