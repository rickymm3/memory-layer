# Memory-Layer Architecture

## 1. Current Prototype

### Single-User Local System
The memory-layer prototype is currently a **single-user, local-only system** designed to explore how an LLM can maintain durable, reconciled memories across a conversation session.

### Key Components

**LLM Inference**
- Ollama running locally with qwen3:8b model for chat responses
- Embeddings generated with qwen3-embedding:latest (4096-dimensional vectors)
- No cloud dependency
- Streaming response for reconciliation analysis

**Storage**
- PostgreSQL database
- pgvector extension for semantic similarity search
- Cosine distance metric for embedding retrieval

**Memory Atoms as Source of Truth**
- Each `memory_atom` row in Postgres is append-first; rows can be manually edited through update tools
- `id`: UUID, auto-generated
- `content`: Full, clean, standalone memory sentence—the source of truth
- `context_summary`: Compact version for inclusion in prompts (may omit scope prefix)
- `memory_type`: fact, preference, instruction, correction, decision, opinion, relationship, warning, temporary_context
- `scope`: Domain applicability (null for global, user, memory-layer-project, or custom)
- `confidence`: float 0–1 (how certain the LLM was at extraction)
- `importance`: float 0–1 (how useful this memory is likely to be)
- `embedding`: 4096-d vector (pgvector)
- `embedding_model`: qwen3-embedding:latest (for reproducibility)
- `created_at`: timestamp

**Embeddings Are Semantic Pointers, Not Truth**
- Embeddings enable fast similarity search for related memories
- They are **not** the canonical representation of meaning
- The `content` field is the sole source of truth for what the memory says
- Reconciliation uses embeddings to find candidates for comparison, then LLM analyzes actual text

**Write Policy Controls Storage Decisions**
- Reconciliation determines the relationship between each candidate and existing memories.
- The write policy then routes each candidate to one of three paths:
  1. **Auto-store and report**: low-risk candidates (`new`, `refinement` with no conflict) may be stored automatically; every automatic write is reported to the user with memory id, content, type, scope, and whether a linked signal was created. No silent writes.
  2. **Ask for confirmation**: conflicts, opinion changes, sensitive or personal claims, and high-impact project instructions route to manual review before any write occurs.
  3. **Skip**: duplicates and reinforcements are discarded; no write occurs and no report is produced.
- All writes — automatic or confirmed — must store a `memory_atom` and a linked `memory_signal` in a single transaction.

---

## 2. Current Memory Flow

### Step-by-Step Process

1. **User Input**
   - User sends a message to the chat session

2. **Assistant Answer**
   - LLM generates a response using retrieved memories as context
   - Retrieved memories are injected into the prompt as context only
   - Current system may sanitize commitment language to avoid overcommitment
   - Future architecture: reconciliation-aware answer generation where assistant has reconciliation state before final wording

3. **Memory Candidate Extraction**
   - LLM analyzes the user message (not the assistant answer) for extractable memory candidates
   - Extraction prompt enforces: must be user-sourced, durable, and reusable
   - One-off questions → empty candidates
   - Candidates include: type, scope, confidence, importance, reason

4. **Embedding Retrieval**
   - Each candidate is embedded using the same model
   - pgvector similarity search returns top N related existing memories
   - Relatedness is based on embedding cosine distance, not truth

5. **LLM Reconciliation**
   - For each candidate, LLM compares it against retrieved related memories
   - LLM outputs a relationship classification:
     - `duplicate`: same meaning, no new info
     - `reinforcement`: same meaning but stronger/repeated phrasing
     - `refinement`: compatible but more specific or clearer
     - `conflict`: contradicts an existing fact, instruction, or decision
     - `opinion_change`: contradicts or shifts previous opinion/preference/stance
     - `new`: no meaningful related memory exists
   - LLM also recommends action: skip, store_new, update_existing, or ask_user

6. **Duplicate/Reinforcement Filtering**
   - Candidates with `duplicate` or `reinforcement` relationships are skipped (not stored)
   - Refinement, conflict, opinion_change, and new are marked as storable

7. **Write Policy Decision**
   - The reconciliation relationship determines which path each storable candidate takes:
     - `new` or `refinement` (low risk) → **auto-store path**: the memory is written immediately; a write report is returned to the user containing: memory id, content, type, scope, and whether a linked signal was created.
     - `conflict`, `opinion_change`, sensitive/personal claims, or high-impact instructions → **confirmation path**: user is shown the candidate, its relationship to existing memories, and the reconciliation reason, and must approve or reject before any write occurs.
   - **No silent writes**: every storage event — auto or confirmed — is reported to the user.

8. **Exact Match Guard**
   - Before storage, final check for exact content match
   - If found, storage is skipped (duplicate detected at write time)

9. **Storage**
   - Approved candidate is stored as a new `memory_atom` row
   - Embedding is computed and stored
   - Current atoms are append-first and manually editable through update/delete tools
   - Future signal records (Phase 2+) should be immutable evidence; future atoms may be recomputed from signals

---

## 3. Reconciliation Relationships

### Duplicate
**Definition**: The candidate expresses the same meaning as an existing memory with no meaningful new information.

**Example**:
- Existing: "The user prefers dark mode."
- Candidate: "Dark mode is the user's preference."
- Relationship: duplicate
- Action: skip

---

### Reinforcement
**Definition**: The candidate reinforces an existing memory through repetition or stronger phrasing, but adds no new substantive information.

**Example**:
- Existing: "The user prefers dark mode."
- Candidate: "The user really loves dark mode."
- Relationship: reinforcement
- Action: skip (for now—may be useful for confidence boosting in future)

---

### Refinement
**Definition**: The candidate is compatible with existing memory but more specific, clearer, or adds nuanced detail.

**Example**:
- Existing: "For this project, reconciliation should compare candidates with stored memories."
- Candidate: "For this project, reconciliation should compare new candidates with related stored memories before storing."
- Relationship: refinement
- Action: store_new

---

### Conflict
**Definition**: The candidate directly contradicts an existing fact, instruction, or decision.

**Example**:
- Existing: "The user likes pineapple pizza." (preference)
- Candidate: "The user hates pineapple pizza."
- Relationship: conflict
- Reason: Contradicts existing stored preference
- Action: ask_user/review before storage (user decides whether to approve and store as new atom)

---

### Opinion Change
**Definition**: The candidate contradicts or significantly shifts a previous opinion, preference, or stance, indicating an explicit change of mind rather than factual error.

**Example**:
- Existing: "The user prefers manual memory confirmation." (opinion)
- Candidate: "The user is now comfortable with automatic memory suggestions." (opinion shift)
- Relationship: opinion_change
- Action: ask_user/review before storage (user decides whether to approve and store as new atom)

---

### New
**Definition**: The candidate introduces information with no meaningful related existing memory.

**Example**:
- Candidate: "The user is a Python developer."
- Related memories: [facts about database, embeddings, Ollama setup]
- Relationship: new (no personal facts about the user)
- Action: store_new

---

## 4. Why Flat Memory Atoms Are Not Enough

The current prototype uses a single primary row per memory—an **atom**. This works as a proof-of-concept but does not yet separate evidence from aggregate interpretation and has fundamental limitations:

### Single Representation Problem
- Each memory is a single text snapshot
- No separation between evidence and interpretation
- Difficult to track source, recency, or confidence evolution

### Multi-Source Consensus Problem
- With only one user, this is not yet an issue
- In multi-user mode, there is no way to weight or aggregate opinions from multiple sources
- A claim from one user and a contradictory claim from another user would both need storage but with no aggregation logic

### Opinion Drift and Historical Context
- A simple field update would lose the historical record of how opinions changed
- Conflict detection is one-off, not a running record of divergence over time

### Repetition and Source Fatigue
- If the same user states "the user prefers dark mode" 100 times, each instance competes for relevance equally
- There is no concept of diminishing returns from the same source repeating the same claim

### Confidence Weighting
- `confidence` is static at creation time
- There is no mechanism to adjust confidence based on:
  - How many independent sources agree
  - Recency of the most recent supporting signal
  - Degree of disagreement in the system

### Spam and Sockpuppet Resistance
- In multi-user mode, a bad actor could submit many corroborating signals for a false claim
- Without source identity and independent source weighting, no defense against this

### Stubbornness and Stability
- No measure of how stable a memory is (how often it changes)
- No measure of how contested a memory is (how many sources disagree)

---

## 5. Planned Signal-Based Architecture

### Overview
To address the limitations above, the planned next architecture introduces **memory signals** as raw evidence and **memory atoms** as computed interpretations.

### Memory Signals
**Definition**: Immutable evidence records that store a claim plus its context and source attribution.

**Fields**:
- `id`: UUID, auto-generated
- `source_key`: Source identifier (in single-user: "local_user"; in multi-user: user identity, device fingerprint, or reputation score)
- `source_id`: Optional foreign key or string to track multi-device, multi-session origins
- `memory_type`: fact, preference, instruction, correction, decision, opinion, relationship, warning, temporary_context (same as atoms)
- `scope`: Domain applicability (null for global, user, memory-layer-project, or custom)—independent of source identity
- `content`: The claim text (same format as memory_atom)
- `confidence`: 0–1, how certain the signal source was at creation
- `intensity`: 0–1, how strongly the source asserts this (e.g., "I strongly prefer" vs. "I somewhat prefer")
- `created_at`: When the signal was submitted
- `parent_signal_id`: Optional, if this signal corrects or refines a prior signal from the same source

### Memory Atoms (Updated)
**Definition**: In the signal-based architecture, memory atoms become computed aggregate interpretations of a collection of signals; in the current prototype, they are primary storage.

**Current Fields** (stable through Phase 1):
- `id`, `content`, `context_summary`, `memory_type`, `scope`, `confidence`, `importance`, `embedding`, `embedding_model`, `created_at` (unchanged)

**Future Enhanced Fields** (Phase 2+, after signals are added):
- `support_weight`: Aggregated weight from independent sources supporting this atom
- `opposition_weight`: Aggregated weight from sources contradicting this atom
- `confidence`: Recomputed from signal agreement and recency (replaces static value)
- `disagreement_score`: 0–1, how contested is this memory?
- `stability_score`: 0–1, how often does the aggregate change?
- `last_signal_id`: Foreign key to the most recent signal that updated this atom
- `updated_at`: When the atom was last recomputed from signals

Note: Current atoms do not include `source_key` or `source_id`; those fields belong on `memory_signals` once introduced.

### Single-User Mode (Phase 1–2)
- All signals use `source_key = "local_user"`
- Source fingerprinting is optional (e.g., device-local tracking)
- Write policy controls storage: low-risk new memories may be auto-stored and reported; conflicts, opinion changes, sensitive claims, and high-impact instructions require confirmation before storage
- Current atoms are not stored with source_key; signals will add this in Phase 2

### Multi-User Mode (Phase 5+)
- Signals carry source identity (user ID, device fingerprint, or reputation score) in `source_key` / `source_id`
- Atoms are computed by aggregating signals:
  - Support weight from each independent source counts
  - Same-source repetition has diminishing returns
  - Recency boosts weight for recent signals
  - Disagreement triggers review or lowers confidence
- Multi-user consensus mechanisms can be defined (e.g., majority vote, weighted voting, Byzantine fault tolerance concepts)
- Spam resistance: independent source weighting prevents single actors from inflating consensus

---

## 6. Proposed memory_signals Schema

### Purpose
`memory_signals` are immutable evidence records. Each signal represents one approved extracted claim, preference, correction, instruction, or opinion, recorded before those signals are aggregated into `memory_atoms`. Signals preserve provenance, source identity, and the full extraction context so that future weighting and consensus logic has something to operate on.

### Proposed Schema

```sql
CREATE TABLE memory_signals (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- relationship to atoms and signal chains
    memory_atom_id       UUID REFERENCES memory_atoms(id) ON DELETE SET NULL,
    parent_signal_id     UUID REFERENCES memory_signals(id) ON DELETE SET NULL,

    -- source attribution (identifies who/what produced the signal, not the memory scope)
    source_key           TEXT NOT NULL DEFAULT 'local_user',
    source_type          TEXT NOT NULL DEFAULT 'local',
    source_id            TEXT,           -- future user/device/session/account identifier

    -- claim content
    content              TEXT NOT NULL,  -- full standalone claim sentence
    context_summary      TEXT,           -- compact prompt version
    memory_type          TEXT NOT NULL,
    scope                TEXT,           -- domain applicability: user, memory-layer-project, etc.

    -- optional semantic fields for future weighting
    subject              TEXT,           -- normalized subject, e.g. "pineapple pizza"
    stance               TEXT,           -- positive, negative, neutral, instruction, correction, unknown

    -- reconciliation metadata
    relationship         TEXT,           -- duplicate, reinforcement, refinement, conflict, opinion_change, new
    certainty            FLOAT,          -- source/extraction certainty
    intensity            FLOAT,          -- strength of wording
    confidence           FLOAT,          -- extraction confidence
    importance           FLOAT,

    -- extraction context
    raw_input            TEXT,           -- original user message or compact excerpt (see note below)
    reconciliation_reason TEXT,

    -- extensibility
    metadata             JSONB,

    created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### Field Notes

**source_key / source_id**
Identify the source of the signal, not the memory scope. In single-user mode, `source_key = 'local_user'`. In multi-user mode, these carry user IDs, device fingerprints, or reputation-linked identifiers. `scope` is separate: it describes *where* the memory applies (global, user, project), not *who* produced the signal.

**subject and stance**
Optional in Phase 2. These fields become important for weighting and drift detection in later phases. `subject` is a normalized noun phrase for grouping related signals. `stance` records whether the signal is asserting something positively, negatively, neutrally, or as a correction/instruction.

**memory_atom_id**
Nullable initially. A signal may be approved and stored without immediately being linked to an atom. In Phase 2, the atom is created first and then the signal is linked to it. In Phase 3+, atoms may be recomputed from signals and the link may be established later.

**parent_signal_id**
Used for correction, refinement, and opinion-change chains. When a new signal supersedes a prior one from the same source, `parent_signal_id` points to the older signal. The older signal is left unchanged as historical evidence.

**raw_input**
Treat carefully. For now, storing a compact excerpt or the verbatim sentence that triggered extraction is useful for debugging and reconciliation traceability. Long-term, consider storing only a content hash or a short excerpt rather than full chat messages to limit storage size and avoid retaining sensitive conversational context.

**Immutability**
Signals are append-only. Once written, they should not be updated except for administrative cleanup (e.g., deleting test data or correcting a technical error). Historical signal records are the foundation of provenance; mutating them would invalidate weighting and audit trails.

---

### Planned Indexes

```sql
-- traceability and signal chains
CREATE INDEX idx_memory_signals_memory_atom_id   ON memory_signals(memory_atom_id);
CREATE INDEX idx_memory_signals_parent_signal_id ON memory_signals(parent_signal_id);

-- source filtering: same-source repetition decay and spam-resistance logic
CREATE INDEX idx_memory_signals_source_key  ON memory_signals(source_key);
CREATE INDEX idx_memory_signals_source_type ON memory_signals(source_type);

-- aggregation and domain filtering
CREATE INDEX idx_memory_signals_scope   ON memory_signals(scope);
CREATE INDEX idx_memory_signals_subject ON memory_signals(subject);

-- reconciliation type filtering (conflict, opinion_change, reinforcement queries)
CREATE INDEX idx_memory_signals_relationship ON memory_signals(relationship);

-- recency weighting queries
CREATE INDEX idx_memory_signals_created_at ON memory_signals(created_at);
```

**Index Notes**
- `memory_atom_id` / `parent_signal_id`: Support traceability queries and correction/refinement/opinion-change chain traversal.
- `source_key` / `source_type`: Support same-source repetition decay logic and spam-resistance filtering by source.
- `scope` / `subject`: Support aggregation queries that group signals by domain and normalized subject for Phase 3+.
- `relationship`: Supports efficient filtering of conflict, opinion_change, and reinforcement cases.
- `created_at`: Supports recency weighting queries; signals can be ordered or windowed by age per memory_type half-life.

---

### Phase 2 Minimal Behavior

Phase 2 introduces `memory_signals` storage without changing existing chat or retrieval behavior. The flow for an approved candidate becomes:

1. **User approves candidate** in the manual review step (same as today).
2. **Store `memory_atom`** using the existing current code path; capture the created atom's `id`.
3. **Store `memory_signal`** with `source_key = 'local_user'`, full candidate fields, reconciliation relationship, reason, and `memory_atom_id` already set to the atom `id` from step 2. The signal is written complete—no post-creation mutation needed.
4. **Keep both inserts in one transaction where practical** to avoid orphaned signals.
5. **No weighting or aggregation yet.** Signals accumulate in the table as a historical record.
6. **Existing chat and retrieval behavior is unchanged.** The assistant still queries `memory_atoms` for context; signals are not yet used in retrieval.

This preserves current behavior and signal immutability while building the data foundation needed for Phase 3 signal-to-atom reconciliation and Phase 4 weighted consensus.

---

## 7. Future Weighting Concepts

### Same-Source Repetition Has Diminishing Returns
If user A states "I like pizza" today and again tomorrow, the second signal adds less weight than if user B also states "they like pizza."

**Implementation concept**:
```
support_from_source_a = 1.0 + 0.5 + 0.25 + ...  (geometric decay)
support_from_source_b = 1.0
total_support = 1.75 (from source A) + 1.0 (from source B) = 2.75
```

---

### Independent Sources Increase Consensus More
If user A and user B both agree, the confidence is higher than if only user A repeats the claim 10 times.

**Implementation concept**:
```
consensus_score = unique_sources_agreeing / total_independent_sources
```

---

### Certainty / Intensity Affects Weight
A signal stating "I strongly prefer dark mode (intensity=1.0)" weighs more than "I might prefer dark mode (intensity=0.3)."

**Implementation concept**:
```
signal_weight = base_weight * intensity * recency_factor
```

---

### Recency Affects Weight
A memory stated 5 minutes ago has more influence than one stated 6 months ago (for preferences) or about the same influence (for stable facts).

**Implementation concept**:
```
recency_factor = exp(-(now - signal_time) / half_life)
where half_life depends on memory_type (short for preferences, long for facts)
```

---

### Conflicts Reduce Confidence or Trigger Review
When a new signal contradicts an existing atom, the system can:
1. Lower the confidence of the existing atom
2. Mark the atom as disputed
3. Trigger a manual review request
4. Compute a disagreement_score reflecting how many sources disagree

---

### Opinion Changes Are Historical Shifts, Not Simple Overwrites
When a signal contradicts a previous signal from the same source, the system records:
- The old signal (unchanged, historical)
- The new signal (new row, `parent_signal_id` points to old)
- Atom recomputed: `confidence` reflects recent shift, but historical record is preserved

---

## 8. Near-Term Implementation Plan

### Phase 1: Keep Current System Stable
- Continue using `memory_atoms` as-is for the prototype
- Preserve confirmation requirement for conflicts, opinion changes, sensitive claims, and high-impact instructions
- Gather data and test edge cases with current reconciliation flow

### Phase 2: Introduce Memory Signals Table
- Add `memory_signals` table with fields described in Section 6
- All newly approved candidates are stored as **both a signal and an atom** in a single transaction
- The atom is created first; `memory_atom_id` is set on the signal at creation time (no post-creation mutation)
- Atoms remain the retrieval source; signals accumulate as historical evidence for future aggregation
- Single-user mode: each signal uses `source_key = "local_user"`
- Phase 3 can move toward signal-to-atom recomputation once signals have accumulated

### Phase 3: Signal-to-Atom Reconciliation
- Implement reconciliation logic that reads signals and computes atoms
- Compute initial versions of:
  - `support_weight`: count of agreeing independent sources
  - `opposition_weight`: count of disagreeing sources
  - `confidence`: based on agreement and recency
  - `disagreement_score`: opposition_weight / (support_weight + opposition_weight)
  - `stability_score`: measure of how often the aggregate changes

### Phase 4: Weighting and Consensus
- Implement geometric decay for same-source repetition
- Implement recency factor based on memory_type
- Implement intensity weighting for signals
- Test with multi-signal scenarios

### Phase 5: Multi-User Support (Future)
- Extend source_key to support user IDs and device fingerprints
- Implement multi-user consensus algorithms
- Add spam resistance and Byzantine fault tolerance concepts
- Maintain backward compatibility with single-user mode

---

## Architecture Diagram (Conceptual)

```
┌─────────────────────────────────────────┐
│         User Interaction                │
├─────────────────────────────────────────┤
│ Chat Input → Extraction → Reconciliation│
│ → Write Policy Decision → Storage       │
└──────────────┬──────────────────────────┘
               │
        ┌──────▼──────────┐
        │ Memory Signals  │  (raw evidence, immutable)
        │ (Future: Phase 2)
        │ - source_key    │
        │ - content       │
        │ - confidence    │
        │ - intensity     │
        │ - created_at    │
        └──────┬──────────┘
               │
        ┌──────▼──────────────────────┐
        │ Signal → Atom Reconciliation │
        │ (Future: Phase 3+)           │
        │ Compute:                     │
        │ - support_weight             │
        │ - opposition_weight          │
        │ - confidence (aggregated)    │
        │ - disagreement_score         │
        │ - stability_score            │
        └──────┬──────────────────────┘
               │
        ┌──────▼────────────────┐
        │ Memory Atoms          │
        │ (current: Phase 1)    │
        │ (future: aggregates)  │
        │ - content (truth)     │
        │ - context_summary     │
        │ - embedding           │
        │ - scope               │
        │ - type                │
        └───────────────────────┘
               ▲
               │
        ┌──────┴──────────┐
        │ Postgres        │
        │ pgvector        │
        └─────────────────┘
```

---

## Design Rationale

### Why Signals + Atoms?
- **Signals** preserve historical evidence (provenance, recency, source)
- **Atoms** provide fast retrieval and stable meaning (what we know now)
- **Separation** allows confidence updates without rewriting history

### Why Source Keys on Signals?
- Enable multi-user consensus in the future by tracking which source (user, device) submitted each signal
- Support single-user mode trivially (`source_key = "local_user"`)
- Open door to reputation systems, spam resistance, and Byzantine fault tolerance
- Preserve scope as independent domain applicability (not tied to source identity)

### Why Diminishing Returns on Repetition?
- A single user repeating a claim does not increase consensus as much as a new independent user agreeing
- Protects against echo chambers and stubbornness

### Why Recency Factors?
- Preferences change; old preferences should have lower weight
- Facts tend to be stable; old facts should keep high weight
- Automatic decay prevents stale memories from dominating

---

## Open Questions for Future Iteration

1. **How should the write policy work in multi-user mode?**
   - Should users only confirm their own signals?
   - Should policy thresholds (auto-store vs. review) differ per source trust level?
   - Should there be an approval vote among multiple users for high-impact writes?

2. **How should disagreement be resolved?**
   - Lower confidence? Mark as disputed? Store both versions?

3. **What is the right half-life for different memory types?**
   - Facts: years? Permanent?
   - Preferences: weeks? Months?
   - Temporary context: hours?

4. **How do we avoid signal spam?**
   - Rate limiting? Reputation penalties? Content filtering?

5. **Should atoms be recomputed continuously or on-demand?**
   - Continuous: more up-to-date, higher compute cost
   - On-demand: efficient, but stale until needed

6. **How should we handle device/session fingerprinting?**
   - To distinguish "user on laptop" from "user on phone"?
   - Privacy implications?

---

## Conclusion

The current prototype is a solid foundation for exploring LLM-powered memory reconciliation in a single-user context. The signal-based architecture is designed to extend that foundation toward multi-user consensus, source attribution, and confidence-aware memory aggregation—without breaking the current system or losing any historical data.

The design maintains the principle that **write policy controls storage decisions** (auto-store with report, ask for confirmation, or skip), and that **embeddings are pointers, not truth**. All changes are additive and can be implemented incrementally.
