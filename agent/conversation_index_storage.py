"""Profile-scoped durable cursor and read-only source facade for conversation indexes."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path


class ConversationIndexCursorStore:
    """Durable at-least-once replay cursor owned by Hermes, not the plugin."""

    def __init__(self, hermes_home: Path, index_name: str):
        import hashlib

        digest = hashlib.sha256(index_name.encode("utf-8")).hexdigest()[:20]
        self.root = Path(hermes_home) / "conversation-index"
        self.path = self.root / f"{digest}.cursor.json"
        self.index_name = index_name

    def load(self) -> int:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            cursor = payload.get("cursor")
            if payload.get("index") != self.index_name or isinstance(cursor, bool):
                return 0
            return cursor if isinstance(cursor, int) and cursor >= 0 else 0
        except (OSError, ValueError, TypeError):
            return 0

    def save(self, cursor: int) -> None:
        if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0:
            raise ValueError("cursor must be a non-negative integer")
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(f".tmp.{os.getpid()}.{threading.get_ident()}")
        data = json.dumps({"index": self.index_name, "cursor": cursor}, separators=(",", ":"))
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass


class ConversationIndexSourceFacade:
    """Whitelist the canonical read API exposed to an index plugin."""

    __slots__ = ("_db",)

    def __init__(self, db):
        self._db = db

    def get_conversation_snapshot(self, **kwargs):
        return self._db.get_conversation_snapshot(**kwargs)

    def hydrate_message_references(self, references, **kwargs):
        return self._db.hydrate_message_references(references, **kwargs)

    def list_index_conversation_ids(self, **kwargs):
        return self._db.list_index_conversation_ids(**kwargs)

    def resolve_index_conversation_ids(self, conversation_ids):
        return self._db.resolve_index_conversation_ids(conversation_ids)
