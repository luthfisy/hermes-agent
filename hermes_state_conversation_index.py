"""Transactional derived-index outbox helpers for SessionDB."""

from __future__ import annotations

import time

from conversation_index import (
    ConversationChangeType, canonical_content_hash, canonical_message_index_state,
)


class SessionConversationIndexMixin:
    """Write body-free change records on the caller's canonical transaction."""

    # Core owns feed retention. Consumers may fall behind this retained window and
    # rebuild from canonical history; provider health never pins state.db growth.
    CONVERSATION_CHANGE_RETENTION_ROWS = 50_000
    _CONVERSATION_CHANGE_RETENTION_SWEEP_INTERVAL = 1_000

    @staticmethod
    def _prune_conversation_changes_on(conn, max_rows: int) -> int:
        seq_row = conn.execute(
            "SELECT seq FROM sqlite_sequence WHERE name = 'conversation_changes'"
        ).fetchone()
        high_water = int(seq_row[0]) if seq_row and seq_row[0] is not None else 0
        cutoff = high_water - max_rows
        if cutoff < 1:
            return 0
        cursor = conn.execute(
            "DELETE FROM conversation_changes WHERE sequence <= ?",
            (cutoff,),
        )
        return max(0, int(cursor.rowcount or 0))

    def prune_conversation_changes(self, *, max_rows: int | None = None) -> int:
        """Retain at most the newest ``max_rows`` feed sequence positions.

        Deleting rows never resets SQLite's AUTOINCREMENT high-water mark, so the
        surviving minimum sequence becomes the retained floor and lagging consumers
        deterministically rebuild instead of pinning canonical storage.
        """
        if max_rows is None:
            max_rows = self.CONVERSATION_CHANGE_RETENTION_ROWS
        if isinstance(max_rows, bool) or not isinstance(max_rows, int) or max_rows < 1:
            raise ValueError("max_rows must be a positive integer")
        return int(self._execute_write(
            lambda conn: self._prune_conversation_changes_on(conn, max_rows)
        ) or 0)

    def _maybe_prune_conversation_changes(self, conn, sequence: int) -> None:
        """Bound long-lived writers between normal maintenance sweeps."""
        max_rows = self.CONVERSATION_CHANGE_RETENTION_ROWS
        if sequence <= max_rows:
            return
        interval = self._CONVERSATION_CHANGE_RETENTION_SWEEP_INTERVAL
        if interval < 1 or sequence % interval:
            return
        self._prune_conversation_changes_on(conn, max_rows)

    def _record_message_change(
        self, conn, change_type: ConversationChangeType, conversation_id: str, message_id: int,
    ) -> int:
        if change_type not in {ConversationChangeType.MESSAGE_UPSERT, ConversationChangeType.MESSAGE_STATE}:
            raise ValueError("message outbox record requires a message change type")
        row = conn.execute(
            "SELECT typeof(content) AS storage_type, CAST(content AS BLOB) AS content_bytes, "
            "active, compacted FROM messages WHERE id = ? AND session_id = ?",
            (message_id, conversation_id),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"message {message_id} vanished before change publication")
        state = canonical_message_index_state(row["active"], row["compacted"])
        content_hash = canonical_content_hash(row["storage_type"], row["content_bytes"])
        cursor = conn.execute(
            "INSERT INTO conversation_changes "
            "(change_type, conversation_id, message_id, content_hash, state, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (change_type.value, conversation_id, message_id, content_hash, state.value, time.time()),
        )
        sequence = int(cursor.lastrowid)
        self._maybe_prune_conversation_changes(conn, sequence)
        return sequence

    def _record_conversation_change(
        self, conn, change_type: ConversationChangeType, conversation_id: str,
    ) -> int:
        if change_type not in {
            ConversationChangeType.CONVERSATION_RECONCILE, ConversationChangeType.CONVERSATION_DELETE,
        }:
            raise ValueError("conversation outbox record requires a conversation change type")
        cursor = conn.execute(
            "INSERT INTO conversation_changes (change_type, conversation_id, created_at) VALUES (?, ?, ?)",
            (change_type.value, conversation_id, time.time()),
        )
        sequence = int(cursor.lastrowid)
        self._maybe_prune_conversation_changes(conn, sequence)
        return sequence
