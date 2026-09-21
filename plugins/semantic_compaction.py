"""Discovery for optional semantic compaction proposal capabilities."""

from __future__ import annotations

import importlib.metadata
import logging
from typing import Optional

from semantic_compaction_provider import SemanticCompactor

logger = logging.getLogger(__name__)
ENTRY_POINTS_GROUP = "hermes_agent.semantic_compactors"


def _iter_entry_points():
    try:
        points = importlib.metadata.entry_points()
        if hasattr(points, "select"):
            return list(points.select(group=ENTRY_POINTS_GROUP))
        if isinstance(points, dict):
            return list(points.get(ENTRY_POINTS_GROUP, []))
        return [point for point in points if getattr(point, "group", None) == ENTRY_POINTS_GROUP]
    except Exception:
        logger.debug("Semantic-compactor entry-point scan failed", exc_info=True)
        return []


def find_semantic_compactor_entry_point(name: str):
    clean = str(name or "").strip()
    if not clean:
        return None
    return next((point for point in _iter_entry_points() if point.name == clean), None)


def _coerce_compactor(loaded) -> Optional[SemanticCompactor]:
    if isinstance(loaded, SemanticCompactor):
        return loaded
    if isinstance(loaded, type) and issubclass(loaded, SemanticCompactor):
        return loaded()
    if callable(loaded):
        candidate = loaded()
        return candidate if isinstance(candidate, SemanticCompactor) else None
    return None


def load_semantic_compactor(name: str) -> Optional[SemanticCompactor]:
    point = find_semantic_compactor_entry_point(name)
    if point is None:
        return None
    try:
        compactor = _coerce_compactor(point.load())
    except Exception:
        logger.warning("Semantic compactor '%s' failed to load", name, exc_info=True)
        return None
    if compactor is None:
        logger.warning("Semantic compactor '%s' did not provide a SemanticCompactor", name)
    return compactor
