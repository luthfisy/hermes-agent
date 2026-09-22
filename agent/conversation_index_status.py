"""Operator-safe status bookkeeping for derived conversation-index consumers."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, replace
from typing import Optional


@dataclass(frozen=True)
class ConversationIndexConsumerStatus:
    index_name: str
    state: str = "starting"
    configured: bool = True
    available: Optional[bool] = None
    cursor: int = 0
    feed_floor: int = 1
    feed_high_water: int = 0
    lag: int = 0
    rebuild_required: bool = False
    failures: int = 0
    last_error: Optional[str] = None
    last_error_at: Optional[float] = None
    last_recovery_at: Optional[float] = None
    next_retry_at: Optional[float] = None


class ConversationIndexStatusTracker:
    """Thread-safe transitions for content-free consumer/operator status."""

    def __init__(
        self,
        index_name: str,
        cursor: int,
        *,
        base_backoff: float,
        max_backoff: float,
    ):
        self.base_backoff = base_backoff
        self.max_backoff = max_backoff
        self._lock = threading.Lock()
        self._status = ConversationIndexConsumerStatus(index_name=index_name, cursor=cursor)

    def snapshot(self) -> ConversationIndexConsumerStatus:
        with self._lock:
            return self._status

    def update(self, **changes) -> None:
        with self._lock:
            self._status = replace(self._status, **changes)

    @staticmethod
    def lag(high_water: int, cursor: int) -> int:
        return max(0, int(high_water) - int(cursor))

    def set_bounds(self, bounds, *, cursor: Optional[int] = None, **changes) -> None:
        current = self.snapshot()
        effective_cursor = current.cursor if cursor is None else cursor
        self.update(
            feed_floor=bounds.floor_sequence,
            feed_high_water=bounds.high_water_sequence,
            lag=self.lag(bounds.high_water_sequence, effective_cursor),
            cursor=effective_cursor,
            **changes,
        )

    def retry_delay(self, failures: int) -> float:
        return min(
            self.max_backoff,
            self.base_backoff * (2 ** min(failures - 1, 16)),
        )

    def mark_unavailable(self) -> None:
        current = self.snapshot()
        failures = current.failures + 1
        self.update(
            state="unavailable",
            available=False,
            failures=failures,
            next_retry_at=time.time() + self.retry_delay(failures),
        )

    def record_failure(
        self,
        exc: Exception,
        *,
        state: str = "error",
        available: Optional[bool] = None,
        rebuild_required: bool = False,
    ) -> None:
        current = self.snapshot()
        failures = current.failures + 1
        now = time.time()
        self.update(
            state=state,
            available=current.available if available is None else available,
            rebuild_required=rebuild_required,
            failures=failures,
            last_error=type(exc).__name__,
            last_error_at=now,
            next_retry_at=now + self.retry_delay(failures),
        )

    def record_success(self, cursor: int, bounds, *, recovered: bool = False) -> None:
        previous = self.snapshot()
        recovery = previous.last_recovery_at
        if recovered or previous.failures or previous.last_error is not None:
            recovery = time.time()
        self.set_bounds(
            bounds,
            cursor=cursor,
            state="idle",
            available=True,
            rebuild_required=False,
            failures=0,
            last_error=previous.last_error,
            last_error_at=previous.last_error_at,
            next_retry_at=None,
            last_recovery_at=recovery,
        )
