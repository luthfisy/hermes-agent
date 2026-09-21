---
title: "Conversation Index Roadmap"
description: "Roadmap for asynchronous derived conversation indexing over Hermes-owned canonical transcripts"
---

# Conversation Index Roadmap

Status: **in progress**
Last reviewed: **2026-09-21**

This roadmap replaces the canonical-storage direction explored in PR #117813. Hermes keeps one authoritative transcript in its existing core session store. Optional plugins may build rebuildable derived indexes over that transcript, but they do not own conversation persistence, authorization, routing, lifecycle, deletion, backup, or canonical message bodies.

The first intended consumer is Reliquary. The Hermes contract must remain provider-neutral and useful to any derived memory/search/index implementation.

## Target architecture

```text
CLI / TUI / Desktop / ACP / API / bots / cron
                       |
                       v
             canonical Hermes owner
             SessionDB / SessionAuthority
                       |
          canonical transcript commit
                 /             \
                v               v
       normal Hermes state   durable change feed
       resume/search/etc.          |
                                   v
                         async ConversationIndex
                                   |
                      ids / hashes / vectors /
                      offsets / derived metadata
                                   |
                                   v
                         search -> MessageRef[]
                                   |
                                   v
                       core auth + hydration
```

The index is a cache/derived database. Losing it must never lose a conversation.

## Governing invariants

1. **Hermes remains canonical.** Message bodies and conversation lifecycle stay owned by the normal Hermes session store.
2. **Index failure is non-fatal to chat.** Missing, incompatible, offline, corrupt, or slow plugins cannot prevent transcript persistence, resume, rewind, delete, compaction, backup, or core textual search.
3. **No plugin acknowledgement on the commit path.** A chat turn completes after the canonical Hermes mutation commits; indexing happens afterward.
4. **No duplicate canonical transcript body in the index.** Durable index state contains references, hashes, offsets, vectors, and provider-specific derived metadata, not a second authoritative copy of message text.
5. **Change publication is transactional with transcript mutation.** A canonical mutation and the corresponding feed record succeed or fail together in the same SQLite transaction.
6. **At-least-once feed delivery, idempotent indexing.** Replaying a change sequence is legal and must not duplicate derived state.
7. **Core authorizes and hydrates.** Plugins return stable message references; Hermes verifies profile/conversation access, visibility, hash, and range before returning source text.
8. **Core semantics stay core semantics.** Titles, routing, model policy, approvals, billing, archive state, deletion, rewind, compression lineage, and other session invariants are never delegated to the index.
9. **SQLite default behaviour remains unchanged when no index is configured.** No full-history scans, extra subprocesses, or alternate persistence branches are added to ordinary session operations.
10. **The design must compose with a single gateway owner.** It must not add client-side persistence paths that conflict with the SessionAuthority direction in #106742.

## Explicit non-goals

This work does **not**:

- replace `SessionDB` or SQLite;
- implement an alternate canonical conversation store;
- route ordinary transcript reads/writes through a plugin;
- move backup ownership to a plugin;
- require a memory service for chat durability;
- replace Hermes FTS/session search;
- make a plugin authoritative for authorization or profile isolation; or
- make Reliquary-specific types part of Hermes core.

The previous `ConversationStore` ownership design should not be incrementally repaired on this branch.

## Core contracts

### ConversationChange

The feed exposes lightweight durable mutation records. Exact encoding is an implementation detail, but the semantic record needs enough information for an index to determine what must be added, refreshed, invalidated, or rebuilt.

The Phase 0 contract is frozen in
[Conversation Index Phase 0 Contract](./conversation-index-phase0.md). Its body-free
event vocabulary is `message_upsert`, `message_state`, `conversation_reconcile`, and
`conversation_delete`; message state is `active`, `compacted`, or `inactive`.

```python
@dataclass(frozen=True)
class ConversationChange:
    sequence: int
    change_type: ConversationChangeType
    conversation_id: str
    created_at: float
    message_id: int | None = None
    content_hash: str | None = None
    state: MessageIndexState | None = None
```

`conversation_id` is the physical canonical `sessions.id`; compression-lineage
composition remains a Hermes responsibility. The feed never contains full message text.
Bulk lifecycle operations use `conversation_reconcile` rather than enumerating a
pathological or identity-fragile set of row transitions.

Required event semantics include:

- message append/upsert;
- content replacement or canonical content change;
- active/inactive visibility transition;
- rewind/truncation;
- compaction visibility/handoff changes;
- conversation deletion/tombstone; and
- any later metadata change that materially changes index eligibility.

### MessageReference

Index search returns references, not authoritative content:

```python
@dataclass(frozen=True)
class MessageReference:
    conversation_id: str
    message_id: int
    content_hash: str
    start: int
    end: int
    score: float
    metadata: dict[str, Any]
```

Hermes owns hydration. A reference is dropped if the message is absent, inactive when the caller requires active history, outside the caller's authorized profile/conversation set, hash-mismatched, or has invalid offsets.

The hash is produced by Hermes over the canonical payload representation used for hydration. Plugins do not define hash identity.

### ConversationIndex

The index contract is deliberately smaller than `MemoryProvider` and much smaller than the retired `ConversationStore` contract.

Candidate capabilities:

```python
class ConversationIndex(ABC):
    def is_available(self) -> bool: ...

    def consume_changes(
        self,
        changes: Sequence[ConversationChange],
        *,
        after_cursor: int,
    ) -> int:
        """Durably apply changes and return the last committed sequence."""

    def search(
        self,
        query: str,
        *,
        conversation_ids: Sequence[str] | None,
        limit: int,
    ) -> Sequence[MessageReference]: ...

    def reset_for_rebuild(self, *, snapshot_watermark: int) -> None: ...
```

The selected memory plugin may provide a `ConversationIndex` capability, but the interfaces remain separate: turn-time memory behaviour and transcript-derived indexing are different responsibilities.

## Durable change feed

Add a small canonical outbox table beside the transcript state. The initial schema should remain intentionally narrow, for example:

```sql
CREATE TABLE conversation_changes (
    sequence         INTEGER PRIMARY KEY AUTOINCREMENT,
    change_type      TEXT NOT NULL,
    conversation_id  TEXT NOT NULL,
    message_id       INTEGER,
    content_hash     TEXT,
    state            TEXT,
    created_at       REAL NOT NULL
);
```

The initial feed needs no arbitrary payload column. Conversation-level reconcile/delete
events carry only the physical conversation ID; message events carry only identity, hash,
and state.

### Transaction rule

Every transcript mutation that changes index-visible state writes its feed record on the **same connection and in the same transaction** as the canonical mutation.

Do not implement:

```text
commit transcript
call plugin
write feed record
```

Implement:

```text
BEGIN
mutate canonical transcript
append conversation_changes row(s)
COMMIT

later:
async consumer -> plugin
```

This guarantees that a plugin outage cannot create an unobservable committed transcript mutation.

### Feed retention

The feed is durable but not infinite history.

Core exposes:

- current high-water sequence;
- oldest retained sequence; and
- changes after a cursor.

If a plugin cursor predates the retained floor, incremental replay fails explicitly with "rebuild required." Retention policy is bounded and independent of plugin availability; a dead plugin cannot grow core storage without limit.

## Rebuild protocol

Rebuild is a first-class correctness path, not disaster-only tooling.

Core should be able to create a canonical **snapshot manifest** under one read transaction:

```text
snapshot_watermark
conversation_id
message_id
content_hash
active/visibility state
other index-eligibility metadata
```

The manifest contains no full transcript copy.

The index then:

1. resets/builds a new derived generation;
2. hydrates referenced canonical messages in bounded batches;
3. verifies each hydrated hash against the manifest before indexing it;
4. atomically installs the rebuilt generation;
5. records the snapshot watermark; and
6. replays changes after that watermark.

If a message changes while rebuilding, hash verification may reject the stale manifest entry; the post-watermark feed event supplies the newer state.

## Async ownership and #106742

The correctness of the feed does not depend on #106742 landing: the outbox is generated inside the same canonical `SessionDB` transactions that already own transcript mutations.

Consumer ownership should, however, align with the canonical gateway direction:

- when a gateway/SessionAuthority owns the profile, it owns the live index-consumer loop;
- clients never write plugin index state independently;
- managed workers persist through the owner and do not call the index directly;
- a multiplexed gateway maintains profile-scoped index instances/cursors; and
- secondary profile failure may degrade that profile's index without redirecting it to another profile.

Until the canonical-owner work lands, any interim consumer host must still enforce one active consumer per profile and must remain off the transcript commit path.

## Search integration

Core `SessionDB.search_messages()` and SQLite FTS remain authoritative first-party transcript search.

Derived index search is additive:

```text
memory/vector query
    -> ConversationIndex.search()
    -> MessageReference[]
    -> core authorization + hash/range validation
    -> canonical hydration
    -> caller
```

A plugin result cannot smuggle text, broaden profile visibility, reactivate a rewound message, or resurrect a deleted conversation.

## Compaction integration

Reliquary-style semantic compaction is compatible with this architecture, but it is **not part of the index commit path** and should not inflate the first indexing PR.

Later add a separate optional proposal capability, for example:

```python
@dataclass(frozen=True)
class CompactionProposal:
    conversation_id: str
    source_fingerprint: str
    summary: str
    compacted_message_ids: tuple[int, ...]
    preserved_tail_ids: tuple[int, ...]
    metadata: dict[str, Any]
```

Flow:

```text
Hermes snapshot/fingerprint
    -> optional semantic compactor
    -> CompactionProposal
    -> Hermes validates source fingerprint / active IDs
    -> Hermes performs its normal canonical compaction transaction
    -> change feed records resulting visibility/handoff changes
```

The compactor never closes, rewinds, deletes, or replaces a canonical conversation itself. If unavailable, Hermes keeps its native compaction/fallback behaviour.

# Phase tracker

| Phase | Status | Completion gate |
| --- | --- | --- |
| 0. Contract/baseline | Complete | Ownership and event semantics frozen |
| 1. Transactional feed | Complete | Every target mutation publishes atomically |
| 2. Source + hydration API | Planned | Stable refs can be safely hydrated |
| 3. Async index capability | Planned | Plugin outage never blocks chat |
| 4. Search/reference path | Planned | Derived search cannot bypass core authorization |
| 5. Rebuild/status | Planned | Lost index rebuilds from canonical state |
| 6. Canonical-owner acceptance | Planned | Gateway/worker paths produce one canonical row and one derived entry |
| 7. Semantic compaction proposals | Later/separate | Optional compactor cannot mutate canonical history directly |

## Phase 0 — Contract and baseline

**Work**

- Inventory canonical message mutations in `hermes_state_messages.py`, compression, and deletion/session lifecycle paths.
- Define canonical hash input and visibility semantics.
- Define feed event vocabulary and bulk-operation rules.
- Decide plugin capability registration without changing existing `MemoryProvider` behaviour.
- Record existing SQLite performance/behaviour baselines for ordinary chat, resume, FTS search, rewind, compression, backup, and approvals.

**Exit**

Complete. The frozen inventory, physical identity rules, hash boundary, message-state
semantics, plugin capability decision, and pre-outbox baseline are recorded in
[Conversation Index Phase 0 Contract](./conversation-index-phase0.md). Every known
canonical transcript mutation now has either an explicit Phase 1 feed consequence or an
explicit no-index-visible-change classification.

## Phase 1 — Transactional change feed

**Likely owners**

- `hermes_state_schema.py`
- `hermes_state_messages.py`
- `hermes_state_compression.py`
- `hermes_state_sessions.py`
- focused tests under `tests/hermes_state/`

**Work**

Add the outbox table and small transaction-local helper(s). Modify canonical mutations only enough to append feed records on the same SQLite transaction.

Do not load plugins or start background workers in this phase.

**Required RED tests first**

1. append commits message + feed row atomically;
2. injected feed-write failure rolls back the canonical mutation;
3. rewind/replace/compaction/deletion emit the expected invalidation/tombstone semantics;
4. default session behaviour is otherwise byte/row compatible where observable; and
5. feed contains no message body.

**Status: complete.** The body-free `conversation_changes` outbox is created with the canonical
SQLite schema, and all known canonical transcript mutation paths either append an atomic feed
record on the same transaction or are explicitly feed-invisible under the Phase 0 contract.
Focused feed tests cover atomic rollback, append, repair, replace, rewind, both compaction modes,
content rewrite, import/profile move, deletion, pruning, and empty-session sweeping.

## Phase 2 — Canonical source and hydration API

Add provider-neutral read surfaces for:

- feed floor/high-water and paged changes-after-cursor;
- snapshot manifest for rebuild;
- bounded hydration by stable message reference;
- authorization/visibility/hash/range validation; and
- profile-scoped enumeration.

These APIs expose canonical source material; they do not delegate ownership.

## Phase 3 — ConversationIndex capability and async consumer

Add a narrow optional capability that can be supplied by the configured memory plugin/package.

Requirements:

- plugin availability is checked outside `SessionDB` construction;
- failed initialization marks indexing unavailable/stale without disabling sessions;
- consumption is asynchronous and profile-scoped;
- cursor advancement is durable only after the plugin durably applies the corresponding changes;
- replaying the same sequence is idempotent;
- consumer failure uses bounded backoff and exposes status; and
- no plugin method is called while a transcript write transaction is open.

Existing `MemoryProvider.sync_turn()` must not become a second transcript-ingestion authority for a provider using the change feed. A provider may still use turn hooks for non-authoritative recall/UI behaviour.

## Phase 4 — Search references and hydration

Wire optional vector/memory search through `MessageReference` hydration.

Tests must cover:

- deleted message;
- rewound/inactive message;
- edited message/hash mismatch;
- invalid offsets;
- forged conversation/profile reference; and
- mixed valid/stale results where valid refs still hydrate.

## Phase 5 — Rebuild, lag, and operator status

Add status/rebuild surfaces only after the underlying contract works.

Status should report at least:

- configured index;
- available/unavailable;
- feed floor/high-water;
- plugin cursor;
- lag;
- rebuild-required state; and
- last error/recovery time without leaking user content.

Rebuild must use the snapshot-manifest protocol above.

## Phase 6 — Canonical-owner acceptance

Before declaring the seam stable, test the combined ownership model represented by #106742 or its landed successor:

```text
submit via one surface
resume/read via another
search via index
=> one gateway admission
=> one canonical message
=> one feed mutation
=> one derived index entry
```

Managed-worker transcript persistence must go through canonical owner operations; workers never bypass the feed by writing an index themselves.

## Phase 7 — Optional semantic compaction

Implement only after indexing/rebuild is proven.

Hermes supplies a stable snapshot/fingerprint. The external service proposes a summary/retained-tail transformation. Hermes validates and commits using its existing compaction authority. Native Hermes compaction remains the fallback.

# Suggested patch sequence

1. `conversation-change-contract` — event/ref/hash types and RED tests.
2. `conversation-change-outbox` — schema + transaction-local publication.
3. `conversation-source-hydration` — feed/snapshot/hydration APIs.
4. `conversation-index-plugin` — capability registration and fake index.
5. `conversation-index-consumer` — async replay/status.
6. `conversation-index-search` — ref validation/hydration.
7. `conversation-index-rebuild` — manifest rebuild and retention-floor recovery.
8. separate follow-up: semantic compaction proposal capability.

Each patch should remain reviewable independently. Do not repeat the 51-file cross-surface rewrite from #117813.

# Acceptance tests

The architectural-review cases are the minimum acceptance matrix:

1. **Plugin unavailable at startup:** chat persists and resumes; only derived memory/index search is unavailable.
2. **Plugin dies after canonical commit:** restart/replay yields one transcript row and one index entry.
3. **Lost/corrupt index:** rebuild produces resolvable references from canonical history.
4. **Stale reference:** edit/rewind/delete after indexing causes hydration to reject the old ref.
5. **Profile isolation:** a forged cross-profile result cannot hydrate.
6. **No duplicate transcript storage:** fixture index persists IDs/hashes/offsets/vectors/metadata but no full message body.
7. **Canonical-owner composition:** cross-surface use results in one admission, one canonical message, and one derived index entry.

Additional gates:

- default SQLite chat/resume/search/backup performance does not materially regress with no index configured;
- feed replay survives process restart;
- compaction emits correct visibility/source changes;
- bounded retention forces explicit rebuild rather than silent gaps; and
- all plugin failures are observable but non-fatal to canonical persistence.

# Definition of done

Hermes-side indexing is complete when an optional third-party package can maintain a durable derived conversation index without owning transcript bodies; every index-visible canonical mutation produces a replayable transactional change; a lost index rebuilds from Hermes history; plugin search returns only references that core authorizes and hydrates; plugin failure cannot prevent normal conversation operation; and the design composes with one canonical gateway owner without introducing alternate client persistence paths.

Reliquary-specific ingestion, embeddings, semantic memories, provenance, observations, and compaction policy remain outside Hermes core.
