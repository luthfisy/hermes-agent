"""Transactional derived-index outbox helpers for SessionDB."""

from __future__ import annotations

import hashlib
import time

from conversation_index import ConversationChangeType, MessageIndexState


class SessionConversationIndexMixin:
    """Write body-free change records on the caller's canonical transaction."""

    @staticmethod
    def _message_index_state(active: int, compacted: int) -> MessageIndexState:
        if int(active or 0) == 1:
            return MessageIndexState.ACTIVE
        if int(compacted or 0) == 1:
            return MessageIndexState.COMPACTED
        return MessageIndexState.INACTIVE

    @staticmethod
    def _stored_content_hash(storage_type: str, content_bytes) -> str:
        payload = b"" if content_bytes is None else bytes(content_bytes)
        digest = hashlib.sha256(storage_type.encode("ascii") + b"\0" + payload).hexdigest()
        return f"sha256:{digest}"

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
        state = self._message_index_state(row["active"], row["compacted"])
        content_hash = self._stored_content_hash(row["storage_type"], row["content_bytes"])
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
