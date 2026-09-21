"""Asynchronous derived conversation-index replay over Hermes canonical state."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Optional

from agent.conversation_index_storage import (
    ConversationIndexCursorStore,
    ConversationIndexSourceFacade,
)
from conversation_index import ConversationFeedGapError, ConversationIndex


@dataclass(frozen=True)
class ConversationIndexConsumerStatus:
    index_name: str
    state: str = "starting"
    cursor: int = 0
    failures: int = 0
    last_error: Optional[str] = None
    next_retry_at: Optional[float] = None


class ConversationIndexConsumer:
    """Profile-scoped at-least-once replay worker. No transcript writes call this object."""

    def __init__(
        self,
        *,
        index_name: str,
        index: ConversationIndex,
        db_path: Path,
        cursor_store: ConversationIndexCursorStore,
        profile_name: str,
        hermes_home: Path,
        batch_size: int = 100,
        poll_interval: float = 1.0,
        base_backoff: float = 1.0,
        max_backoff: float = 60.0,
    ):
        self.index_name = index_name
        self.index = index
        self.db_path = Path(db_path)
        self.cursor_store = cursor_store
        self.profile_name = profile_name
        self.hermes_home = Path(hermes_home)
        self.batch_size = batch_size
        self.poll_interval = max(0.05, float(poll_interval))
        self.base_backoff = max(0.05, float(base_backoff))
        self.max_backoff = max(self.base_backoff, float(max_backoff))
        self._db = None
        self._source = None
        self._initialized = False
        self._lock = threading.Lock()
        self._status = ConversationIndexConsumerStatus(
            index_name=index_name, cursor=cursor_store.load(),
        )

    def status(self) -> ConversationIndexConsumerStatus:
        with self._lock:
            return self._status

    def _set_status(self, **changes) -> None:
        with self._lock:
            self._status = replace(self._status, **changes)

    def _ensure_initialized(self) -> bool:
        if not self.index.is_available():
            self._record_unavailable()
            return False
        if self._db is None:
            from hermes_state import SessionDB

            self._db = SessionDB(db_path=self.db_path, read_only=True)
            self._source = ConversationIndexSourceFacade(self._db)
        if not self._initialized:
            try:
                self.index.initialize(
                    self._source,
                    profile_name=self.profile_name,
                    hermes_home=str(self.hermes_home),
                )
            except Exception as exc:
                self._record_failure(exc, state="unavailable")
                return False
            self._initialized = True
        return True

    def _retry_delay(self, failures: int) -> float:
        return min(self.max_backoff, self.base_backoff * (2 ** min(failures - 1, 16)))

    def _record_unavailable(self) -> None:
        failures = self.status().failures + 1
        self._set_status(
            state="unavailable", failures=failures, last_error=None,
            next_retry_at=time.time() + self._retry_delay(failures),
        )

    def _record_failure(self, exc: Exception, *, state: str = "error") -> None:
        failures = self.status().failures + 1
        self._set_status(
            state=state,
            failures=failures,
            last_error=type(exc).__name__,
            next_retry_at=time.time() + self._retry_delay(failures),
        )

    def run_once(self) -> int:
        cursor = self.status().cursor
        try:
            if not self._ensure_initialized():
                return cursor
            bounds = self._db.get_conversation_change_bounds()
            if cursor < bounds.floor_sequence - 1:
                raise ConversationFeedGapError(cursor, bounds.floor_sequence, bounds.high_water_sequence)
            changes = self._db.get_conversation_changes(
                after_sequence=cursor, limit=self.batch_size,
            )
            if not changes:
                self._set_status(
                    state="idle", failures=0, last_error=None, next_retry_at=None, cursor=cursor,
                )
                return cursor

            self._set_status(state="running", next_retry_at=None)
            applied = self.index.consume_changes(changes, after_cursor=cursor)
            delivered = {change.sequence for change in changes}
            if (
                isinstance(applied, bool)
                or not isinstance(applied, int)
                or applied <= cursor
                or applied not in delivered
            ):
                raise ValueError("index returned an invalid committed cursor")
            self.cursor_store.save(applied)
            self._set_status(
                state="idle", cursor=applied, failures=0, last_error=None, next_retry_at=None,
            )
            return applied
        except ConversationFeedGapError:
            self._set_status(
                state="rebuild_required",
                cursor=cursor,
                last_error="ConversationFeedGapError",
                next_retry_at=None,
            )
            return cursor
        except Exception as exc:
            self._record_failure(exc)
            return cursor

    def run(self, stop_event: threading.Event) -> None:
        try:
            while not stop_event.is_set():
                before = self.status()
                self.run_once()
                current = self.status()
                if current.state == "rebuild_required":
                    stop_event.wait(self.max_backoff)
                elif current.failures > before.failures or current.state in {"error", "unavailable"}:
                    delay = max(0.0, (current.next_retry_at or time.time()) - time.time())
                    stop_event.wait(delay)
                else:
                    stop_event.wait(self.poll_interval)
        finally:
            self.close()

    def close(self) -> None:
        try:
            self.index.shutdown()
        except Exception:
            pass
        if self._db is not None:
            self._db.close()
            self._db = None
