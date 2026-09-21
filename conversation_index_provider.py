"""Provider-neutral capability contract for derived conversation indexes."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional, Protocol, Sequence, Tuple


class ConversationIndexSource(Protocol):
    """Read-only canonical source surface supplied to derived indexes."""

    def get_conversation_snapshot(
        self, *, conversation_ids: Optional[Sequence[str]] = None,
    ) -> "ConversationSnapshot": ...

    def hydrate_message_references(
        self, references: Sequence["MessageReference"], **kwargs: Any,
    ) -> Tuple["HydratedMessage", ...]: ...

    def list_index_conversation_ids(
        self, *, after_id: Optional[str] = None, limit: int = 1000,
    ) -> Tuple[str, ...]: ...

    def resolve_index_conversation_ids(
        self, conversation_ids: Sequence[str],
    ) -> Tuple[str, ...]: ...


class ConversationIndexRebuildRequired(RuntimeError):
    """Provider-owned derived state cannot continue incrementally and needs a canonical rebuild."""


class ConversationIndex(ABC):
    """Optional derived transcript index. Hermes remains canonical."""

    def initialize(
        self, source: ConversationIndexSource, *, profile_name: str, hermes_home: str,
    ) -> None:
        """Bind a read-only canonical source. Default implementation needs no setup."""

    @abstractmethod
    def is_available(self) -> bool:
        """Return whether this index can currently consume/search."""

    @abstractmethod
    def consume_changes(
        self, changes: Sequence["ConversationChange"], *, after_cursor: int,
    ) -> int:
        """Durably apply an idempotent batch and return the last committed sequence."""

    @abstractmethod
    def search(
        self, query: str, *, conversation_ids: Optional[Sequence[str]], limit: int,
    ) -> Sequence["MessageReference"]:
        """Return body-free references. Core authorizes and hydrates them."""

    @abstractmethod
    def rebuild_from_snapshot(self, snapshot: "ConversationSnapshot") -> int:
        """Atomically install a canonical snapshot and return its exact committed watermark."""

    def shutdown(self) -> None:
        """Release provider resources. Consumer failures must not affect canonical state."""
