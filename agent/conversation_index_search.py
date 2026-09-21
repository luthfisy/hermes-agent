"""Core-authorized search over optional derived conversation indexes."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Optional

from agent.conversation_index_storage import ConversationIndexSourceFacade
from conversation_index import HydratedMessage, MessageReference
from conversation_index_provider import ConversationIndex


class ConversationIndexSearchService:
    """Validate untrusted index hits against canonical profile state before returning text."""

    _MAX_RESULTS = 256

    def __init__(
        self,
        *,
        index: ConversationIndex,
        db_path: Path,
        profile_name: str,
        hermes_home: Path,
    ):
        from hermes_state import SessionDB

        self.index = index
        self.profile_name = profile_name
        self.hermes_home = Path(hermes_home)
        self._db = SessionDB(db_path=Path(db_path), read_only=True)
        self._source = ConversationIndexSourceFacade(self._db)
        self._closed = False
        try:
            self.index.initialize(
                self._source,
                profile_name=profile_name,
                hermes_home=str(self.hermes_home),
            )
        except Exception:
            self._db.close()
            self._closed = True
            raise

    @staticmethod
    def _validate_limit(limit: int) -> int:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 256:
            raise ValueError("limit must be between 1 and 256")
        return limit

    def _authorized_scope(
        self, conversation_ids: Optional[Iterable[str]],
    ) -> Optional[tuple[str, ...]]:
        if conversation_ids is None:
            return None
        return self._db.resolve_index_conversation_ids(conversation_ids)

    def search(
        self,
        query: str,
        *,
        conversation_ids: Optional[Iterable[str]] = None,
        limit: int = 10,
    ) -> tuple[HydratedMessage, ...]:
        self._validate_limit(limit)
        if not isinstance(query, str) or not query.strip():
            return ()
        if self._closed:
            return ()

        try:
            if not self.index.is_available():
                return ()
            scope = self._authorized_scope(conversation_ids)
            if conversation_ids is not None and not scope:
                return ()
            raw = self.index.search(
                query,
                conversation_ids=scope,
                limit=limit,
            )
            refs = []
            for position, item in enumerate(raw or ()):
                if position >= self._MAX_RESULTS:
                    break
                if isinstance(item, MessageReference):
                    refs.append(item)
                    if len(refs) >= limit:
                        break
            if not refs:
                return ()
            return self._db.hydrate_message_references(
                refs,
                authorized_conversation_ids=scope,
                include_inactive=False,
                limit=limit,
            )
        except Exception:
            return ()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.index.shutdown()
        except Exception:
            pass
        self._db.close()
