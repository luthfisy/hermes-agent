---
title: "Conversation Index Phase 0 Contract"
description: "Frozen ownership, identity, mutation, and event semantics for derived conversation indexing"
---

# Conversation Index Phase 0 Contract

Status: **frozen for Phase 1**
Last reviewed: **2026-09-21**

This document freezes the Phase 0 mutation inventory and semantic contract behind the
[Conversation Index Roadmap](./conversation-index-roadmap.md). Phase 1 must not publish an
outbox event that contradicts this inventory without updating the contract and tests first.

## Canonical identity

`ConversationChange.conversation_id` is the **physical canonical `sessions.id`** that owns
the `messages` row. It is not a plugin-defined logical thread ID.

Compression may rotate one user-visible conversation across parent/child session rows.
Hermes owns that lineage. Search and hydration may expand a logical conversation into its
physical compression lineage before index search; plugins do not infer or own lineage.

`message_id` is the physical `messages.id` for that session generation. Compaction clones
or resequences rows with new IDs, so bulk compaction uses conversation reconciliation.

## Canonical hash input

`content_hash` is an opaque Hermes-produced hash of canonical stored `messages.content`,
including its SQLite value type. Plugins persist and return the hash but do not compute or
interpret it.

The hash excludes presentation and replay sidecars: `display_metadata`, reactions,
`api_content`, reasoning checkpoints, display ordering, token counts, and session metadata.
Changes only to those fields are not index-visible in v1.

Phase 2 will expose the concrete hash helper and hydration representation. Changing that
representation later requires an explicit rebuild/version transition.

## Message state

| Canonical row | Index state | Default search eligibility |
| --- | --- | --- |
| `active = 1` | `active` | eligible |
| `active = 0, compacted = 1` | `compacted` | eligible as archived evidence |
| `active = 0, compacted = 0` | `inactive` | excluded unless explicitly requested |

`active` wins if a malformed legacy row has both flags set. Hard-deleted rows have no
message state; their removal is communicated by reconcile or conversation deletion.

## Event vocabulary

- `message_upsert` — a new message or canonical `messages.content` change; carries message
  ID, content hash, and current state.
- `message_state` — visibility/compaction-state change without a content change; carries
  message ID, content hash, and new state.
- `conversation_reconcile` — reconcile one physical session against canonical state. Use
  for destructive replacement, compaction/resequencing, imports, and bounded bulk mutation.
- `conversation_delete` — the physical session no longer exists; remove its derived entries.

Conversation-level events carry no message ID, hash, state, or body.

## Mutation inventory

| Canonical mutation path | Phase 1 consequence |
| --- | --- |
| `append_message` | `message_upsert` for inserted row |
| `append_delegation_delivery` | upsert on insert; no event on idempotent hit |
| `append_messages_batch` | upsert inserted rows |
| blank-row repair in `resolve_and_repair_transcript_batch` | upsert repaired row |
| `replace_messages(..., archive_dropped=True)` | state events for dropped suffix + upserts for inserted suffix |
| destructive `replace_messages` | `conversation_reconcile` |
| `archive_and_compact` | `conversation_reconcile` |
| rotating `publish_compression_child` | reconcile new child; parent message content is unchanged |
| `rewind_to_message` | state events for rewound rows + upsert handoff replacement if inserted |
| `set_user_message_content` | `message_upsert` |
| `clear_messages` | reconcile to empty transcript |
| `purge_stale_tool_call_markers` | upsert each content-cleared row |
| `import_sessions` | reconcile each newly imported session |
| `import_moved_session` | reconcile destination session |
| `delete_moved_session` | delete source conversation |
| `delete_session` and delegate cascade | delete every removed physical session |
| `delete_sessions` | delete every removed physical session |
| `prune_sessions` | delete every removed physical session |
| `delete_empty_sessions` / `delete_session_if_empty` | delete removed physical session |
| schema/profile repair reconstructing transcript rows | reconcile affected sessions or require explicit rebuild |

The private `_insert_message_rows` and `_clone_message_rows` helpers are not publication
owners. Their enclosing canonical operation owns the feed events, preventing double publish.

## Explicitly not index-visible in v1

- display kind/order/identity/metadata changes and reactions;
- `api_content` sidecar backfills;
- Codex/reasoning checkpoint cleanup;
- token/usage/cost/session counters;
- titles, pin/read/hidden/archive flags, routing metadata, model policy, and approvals;
- FTS projection rebuilds/triggers; and
- repair probes that roll back and leave canonical rows unchanged.

Session-level access and visibility remain core authorization concerns. Hermes narrows the
physical session-ID set before index search and validates returned references during hydration.

## Capability registration

`ConversationIndex` remains separate from `MemoryProvider`. Phase 3 uses a dedicated
`hermes_agent.conversation_indexes` plugin entry-point group rather than adding transcript
index methods to `MemoryProvider`.

A package may register both capabilities, but either may be absent or fail independently.
Index construction and availability are never part of the `SessionDB` commit path.

## Pre-outbox baseline

Focused baseline on Windows before any feed/schema mutation:

- 136 passed;
- 9 skipped;
- 4 failed, all in existing backup/import Windows or non-default-`HERMES_HOME` expectations;
- append, rewind, compression, search, session archiving, and approvals coverage passed.

Command scope: append batch, compression watermark, composite rewind, relaxed search,
session archiving, approvals suggest, and backup suites. Contract tests separately pass 6/6.
