"""Canonical snapshot rebuild helper for derived conversation indexes."""

from __future__ import annotations

from conversation_index_provider import ConversationIndex


def rebuild_conversation_index(index: ConversationIndex, db, cursor_store) -> int:
    """Install one canonical snapshot and publish its exact watermark as the replay cursor."""
    snapshot = db.get_conversation_snapshot()
    committed = index.rebuild_from_snapshot(snapshot)
    if (
        isinstance(committed, bool)
        or not isinstance(committed, int)
        or committed != snapshot.watermark
    ):
        raise ValueError("index rebuild must commit the exact canonical snapshot watermark")
    cursor_store.save(committed)
    return committed
