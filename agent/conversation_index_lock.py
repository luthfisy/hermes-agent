"""Cross-process ownership lock for one live conversation-index consumer per profile."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path


class ProfileConversationIndexLock:
    """Non-blocking lock held for the lifetime of the active profile consumer."""

    def __init__(self, hermes_home: Path, index_name: str):
        digest = hashlib.sha256(index_name.encode("utf-8")).hexdigest()[:20]
        self.path = Path(hermes_home) / "conversation-index" / f"{digest}.consumer.lock"
        self._handle = None

    def try_acquire(self) -> bool:
        if self._handle is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+b")
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self._handle = handle
        return True

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            handle.close()
