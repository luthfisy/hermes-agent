"""Discovery for optional derived conversation-index capabilities."""

from __future__ import annotations

import importlib.metadata
import logging
from typing import Optional

from conversation_index import ConversationIndex

logger = logging.getLogger(__name__)

ENTRY_POINTS_GROUP = "hermes_agent.conversation_indexes"


def _iter_entry_points():
    try:
        points = importlib.metadata.entry_points()
        if hasattr(points, "select"):
            return list(points.select(group=ENTRY_POINTS_GROUP))
        if isinstance(points, dict):
            return list(points.get(ENTRY_POINTS_GROUP, []))
        return [point for point in points if getattr(point, "group", None) == ENTRY_POINTS_GROUP]
    except Exception:
        logger.debug("Conversation-index entry-point scan failed", exc_info=True)
        return []


def find_conversation_index_entry_point(name: str):
    """Return a matching entry point without importing provider code."""
    clean = str(name or "").strip()
    if not clean:
        return None
    return next((point for point in _iter_entry_points() if point.name == clean), None)


def _coerce_index(loaded) -> Optional[ConversationIndex]:
    if isinstance(loaded, ConversationIndex):
        return loaded
    if isinstance(loaded, type) and issubclass(loaded, ConversationIndex):
        return loaded()
    if callable(loaded):
        candidate = loaded()
        return candidate if isinstance(candidate, ConversationIndex) else None
    return None


def load_conversation_index(name: str) -> Optional[ConversationIndex]:
    """Load the separately registered index capability for a configured provider name."""
    point = find_conversation_index_entry_point(name)
    if point is None:
        return None
    try:
        index = _coerce_index(point.load())
    except Exception:
        logger.warning("Conversation index '%s' failed to load", name, exc_info=True)
        return None
    if index is None:
        logger.warning(
            "Conversation index entry point '%s' did not provide a ConversationIndex", name,
        )
    return index
