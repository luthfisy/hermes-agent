"""Synchronous read path for optional derived conversation-index search."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable, Optional

from agent.conversation_index_search import ConversationIndexSearchService
from conversation_index import HydratedMessage
from plugins.conversation_index import load_conversation_index

logger = logging.getLogger(__name__)


def search_conversation_index(
    *,
    provider_name: str,
    query: str,
    db_path: Path,
    hermes_home: Optional[Path] = None,
    profile_name: Optional[str] = None,
    conversation_ids: Optional[Iterable[str]] = None,
    limit: int = 10,
) -> tuple[HydratedMessage, ...]:
    """Search an optional index, then let core authorize and hydrate every returned reference."""
    clean = str(provider_name or "").strip()
    if not clean:
        return ()

    index = load_conversation_index(clean)
    if index is None:
        return ()

    searcher = None
    try:
        if not index.is_available():
            return ()
        if hermes_home is None:
            from hermes_constants import get_hermes_home

            hermes_home = get_hermes_home()
        if profile_name is None:
            try:
                from hermes_cli.profiles import get_active_profile_name

                profile_name = get_active_profile_name()
            except Exception:
                profile_name = "default"

        searcher = ConversationIndexSearchService(
            index=index,
            db_path=Path(db_path),
            profile_name=profile_name,
            hermes_home=Path(hermes_home),
        )
        return searcher.search(
            query,
            conversation_ids=conversation_ids,
            limit=limit,
        )
    except Exception:
        logger.debug("Conversation-index search unavailable", exc_info=True)
        return ()
    finally:
        if searcher is not None:
            searcher.close()
        else:
            try:
                index.shutdown()
            except Exception:
                pass
