"""Asynchronous derived conversation-index replay over Hermes canonical state."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Optional

from agent.conversation_index_rebuild import rebuild_conversation_index
from agent.conversation_index_status import ConversationIndexConsumerStatus, ConversationIndexStatusTracker
from agent.conversation_index_storage import ConversationIndexCursorStore, ConversationIndexSourceFacade
from conversation_index import ConversationFeedGapError
from conversation_index_provider import ConversationIndex, ConversationIndexRebuildRequired


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
        self._provider_cursor_validated = False
        self._status_tracker = ConversationIndexStatusTracker(
            index_name,
            cursor_store.load(),
            base_backoff=self.base_backoff,
            max_backoff=self.max_backoff,
        )

    def status(self) -> ConversationIndexConsumerStatus:
        return self._status_tracker.snapshot()

    def _ensure_initialized(self) -> bool:
        try:
            available = bool(self.index.is_available())
        except Exception as exc:
            self._status_tracker.record_failure(exc, state="unavailable", available=False)
            return False
        if not available:
            self._status_tracker.mark_unavailable()
            return False

        self._status_tracker.update(available=True)
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
                self._status_tracker.record_failure(exc, state="unavailable", available=False)
                return False
            self._initialized = True
        return True

    def _rebuild(self) -> int:
        cursor = self.status().cursor
        try:
            bounds = self._db.get_conversation_change_bounds()
            self._status_tracker.set_bounds(
                bounds,
                state="rebuilding",
                available=True,
                rebuild_required=True,
                next_retry_at=None,
            )
            committed = rebuild_conversation_index(self.index, self._db, self.cursor_store)
            self._provider_cursor_validated = True
            latest_bounds = self._db.get_conversation_change_bounds()
            self._status_tracker.record_success(committed, latest_bounds, recovered=True)
            return committed
        except Exception as exc:
            self._status_tracker.record_failure(
                exc,
                state="rebuild_required",
                available=True,
                rebuild_required=True,
            )
            return cursor

    def run_once(self) -> int:
        cursor = self.status().cursor
        try:
            if not self._ensure_initialized():
                return cursor

            bounds = self._db.get_conversation_change_bounds()
            self._status_tracker.set_bounds(bounds, available=True)

            if self.status().rebuild_required:
                return self._rebuild()
            if cursor < bounds.floor_sequence - 1 or cursor > bounds.high_water_sequence:
                return self._rebuild()

            if not self._provider_cursor_validated:
                try:
                    self.index.validate_cursor(cursor)
                except ConversationIndexRebuildRequired:
                    return self._rebuild()
                self._provider_cursor_validated = True

            changes = self._db.get_conversation_changes(
                after_sequence=cursor,
                limit=self.batch_size,
            )
            if not changes:
                self._status_tracker.record_success(cursor, bounds)
                return cursor

            self._status_tracker.update(state="running", next_retry_at=None)
            try:
                applied = self.index.consume_changes(changes, after_cursor=cursor)
            except ConversationIndexRebuildRequired:
                return self._rebuild()

            delivered = {change.sequence for change in changes}
            if (
                isinstance(applied, bool)
                or not isinstance(applied, int)
                or applied <= cursor
                or applied not in delivered
            ):
                raise ValueError("index returned an invalid committed cursor")

            self.cursor_store.save(applied)
            latest_bounds = self._db.get_conversation_change_bounds()
            self._status_tracker.record_success(applied, latest_bounds)
            return applied
        except ConversationFeedGapError:
            return self._rebuild()
        except Exception as exc:
            self._status_tracker.record_failure(exc)
            return cursor

    def run(self, stop_event: threading.Event) -> None:
        try:
            while not stop_event.is_set():
                before = self.status()
                self.run_once()
                current = self.status()
                if current.state == "rebuild_required":
                    delay = max(0.0, (current.next_retry_at or time.time()) - time.time())
                    stop_event.wait(delay or self.max_backoff)
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
