"""Canonical read surfaces for derived conversation indexes."""

from __future__ import annotations

import contextlib
import sqlite3
from typing import Iterable, Optional, Sequence

from conversation_index import (
    ConversationChange,
    ConversationChangeType,
    ConversationFeedBounds,
    ConversationFeedGapError,
    ConversationSnapshot,
    MessageIndexState,
    SnapshotMessage,
    canonical_content_hash,
    canonical_hydration_text,
    canonical_message_index_state,
)


class SessionConversationSourceMixin:
    """Expose bounded canonical source data without delegating ownership."""

    _INDEX_READ_LIMIT = 1000

    @staticmethod
    def _index_chunks(values: Sequence, size: int = 900):
        for start in range(0, len(values), size):
            yield values[start:start + size]

    @staticmethod
    def _validate_index_limit(limit: int, maximum: int) -> int:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > maximum:
            raise ValueError(f"limit must be between 1 and {maximum}")
        return limit

    @staticmethod
    def _feed_bounds_on(conn) -> ConversationFeedBounds:
        seq_row = conn.execute(
            "SELECT seq FROM sqlite_sequence WHERE name = 'conversation_changes'"
        ).fetchone()
        high_water = int(seq_row[0]) if seq_row and seq_row[0] is not None else 0
        floor_row = conn.execute("SELECT MIN(sequence) FROM conversation_changes").fetchone()
        floor = int(floor_row[0]) if floor_row and floor_row[0] is not None else high_water + 1
        return ConversationFeedBounds(floor_sequence=floor, high_water_sequence=high_water)

    def _read_index_snapshot(self, fn):
        def _run(conn):
            owns_transaction = not conn.in_transaction
            if owns_transaction:
                conn.execute("BEGIN")
            try:
                return fn(conn)
            finally:
                if owns_transaction and conn.in_transaction:
                    with contextlib.suppress(sqlite3.Error):
                        conn.execute("ROLLBACK")

        return self._read_retrying_ioerr(_run)

    def get_conversation_change_bounds(self) -> ConversationFeedBounds:
        return self._read_index_snapshot(self._feed_bounds_on)

    def get_conversation_changes(self, *, after_sequence: int, limit: int = 100) -> tuple[ConversationChange, ...]:
        if isinstance(after_sequence, bool) or not isinstance(after_sequence, int) or after_sequence < 0:
            raise ValueError("after_sequence must be a non-negative integer")
        self._validate_index_limit(limit, self._INDEX_READ_LIMIT)

        def _read(conn):
            bounds = self._feed_bounds_on(conn)
            if after_sequence < bounds.floor_sequence - 1:
                raise ConversationFeedGapError(
                    after_sequence, bounds.floor_sequence, bounds.high_water_sequence,
                )
            rows = conn.execute(
                "SELECT sequence, change_type, conversation_id, message_id, content_hash, state, created_at "
                "FROM conversation_changes WHERE sequence > ? ORDER BY sequence LIMIT ?",
                (after_sequence, limit),
            ).fetchall()
            return tuple(
                ConversationChange(
                    sequence=int(row["sequence"]),
                    change_type=ConversationChangeType(row["change_type"]),
                    conversation_id=row["conversation_id"],
                    created_at=float(row["created_at"]),
                    message_id=row["message_id"],
                    content_hash=row["content_hash"],
                    state=MessageIndexState(row["state"]) if row["state"] is not None else None,
                )
                for row in rows
            )

        return self._read_index_snapshot(_read)

    def list_index_conversation_ids(
        self, *, after_id: Optional[str] = None, limit: int = 1000,
    ) -> tuple[str, ...]:
        self._validate_index_limit(limit, self._INDEX_READ_LIMIT)
        if after_id is not None and (not isinstance(after_id, str) or not after_id):
            raise ValueError("after_id must be a non-empty string")
        sql = "SELECT id FROM sessions"
        params: list = []
        if after_id is not None:
            sql += " WHERE id > ?"
            params.append(after_id)
        sql += " ORDER BY id LIMIT ?"
        params.append(limit)
        return tuple(row["id"] for row in self._read_all(sql, params))

    @staticmethod
    def _normalize_index_conversation_ids(conversation_ids: Optional[Iterable[str]]) -> Optional[tuple[str, ...]]:
        if conversation_ids is None:
            return None
        return tuple(sorted({value for value in conversation_ids if isinstance(value, str) and value}))

    def _existing_index_conversation_ids(self, conn, requested: Optional[tuple[str, ...]]) -> tuple[str, ...]:
        if requested is None:
            return tuple(row["id"] for row in conn.execute("SELECT id FROM sessions ORDER BY id").fetchall())
        found: list[str] = []
        for chunk in self._index_chunks(requested):
            placeholders = ",".join("?" for _ in chunk)
            found.extend(
                row["id"] for row in conn.execute(
                    f"SELECT id FROM sessions WHERE id IN ({placeholders}) ORDER BY id", chunk,
                ).fetchall()
            )
        return tuple(sorted(found))

    def resolve_index_conversation_ids(self, conversation_ids: Iterable[str]) -> tuple[str, ...]:
        """Intersect an explicit caller scope with conversations in this profile."""
        requested = self._normalize_index_conversation_ids(conversation_ids)
        if not requested:
            return ()
        return self._read_index_snapshot(
            lambda conn: self._existing_index_conversation_ids(conn, requested)
        )

    def get_conversation_snapshot(
        self, *, conversation_ids: Optional[Iterable[str]] = None,
    ) -> ConversationSnapshot:
        requested = self._normalize_index_conversation_ids(conversation_ids)

        def _read(conn):
            watermark = self._feed_bounds_on(conn).high_water_sequence
            existing = self._existing_index_conversation_ids(conn, requested)
            if not existing:
                return ConversationSnapshot(watermark=watermark, conversation_ids=(), messages=())

            messages = []
            for chunk in self._index_chunks(existing):
                placeholders = ",".join("?" for _ in chunk)
                cursor = conn.execute(
                    "SELECT session_id, id, typeof(content) AS storage_type, "
                    "CAST(content AS BLOB) AS content_bytes, content, active, compacted, role, timestamp "
                    f"FROM messages WHERE session_id IN ({placeholders}) "
                    "AND (active = 1 OR compacted = 1) ORDER BY session_id, id",
                    chunk,
                )
                for row in cursor:
                    text = canonical_hydration_text(self._decode_content(row["content"]))
                    messages.append(SnapshotMessage(
                        conversation_id=row["session_id"],
                        message_id=int(row["id"]),
                        content_hash=canonical_content_hash(row["storage_type"], row["content_bytes"]),
                        state=canonical_message_index_state(row["active"], row["compacted"]),
                        text_length=len(text), role=row["role"], timestamp=float(row["timestamp"]),
                    ))
            return ConversationSnapshot(
                watermark=watermark, conversation_ids=existing, messages=tuple(messages),
            )

        return self._read_index_snapshot(_read)
