"""Transactional derived-index outbox helpers for SessionDB."""

from __future__ import annotations

import time

from conversation_index import (
    ConversationChangeType, canonical_content_hash, canonical_message_index_state,
)


class SessionConversationIndexMixin:
    """Write body-free change records on the caller's canonical transaction."""

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
        return int(cursor.lastrowid)

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
        return int(cursor.lastrowid)
