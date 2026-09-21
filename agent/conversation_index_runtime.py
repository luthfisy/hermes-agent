"""Profile-scoped runtime ownership for optional derived conversation indexes."""

from __future__ import annotations

import atexit
import threading
from pathlib import Path
from typing import Optional

from agent.conversation_index_consumer import (
    ConversationIndexConsumer,
    ConversationIndexConsumerStatus,
)
from agent.conversation_index_lock import ProfileConversationIndexLock
from agent.conversation_index_runtime_handle import ConversationIndexRuntimeHandle
from agent.conversation_index_storage import ConversationIndexCursorStore
from plugins.conversation_index import (
    find_conversation_index_entry_point,
    load_conversation_index,
)


_RUNTIMES: dict[tuple[str, str], ConversationIndexRuntimeHandle] = {}
_RUNTIMES_LOCK = threading.Lock()


def _open_consumer(
    *, provider_name: str, index, db_path: Path, hermes_home: Path, profile_name: str,
) -> ConversationIndexConsumer:
    return ConversationIndexConsumer(
        index_name=provider_name,
        index=index,
        db_path=db_path,
        cursor_store=ConversationIndexCursorStore(hermes_home, provider_name),
        profile_name=profile_name,
        hermes_home=hermes_home,
    )


def _status(provider_name: str, state: str, *, error: Optional[str] = None):
    return ConversationIndexConsumerStatus(
        index_name=provider_name, state=state, last_error=error,
    )


def _bootstrap_once(
    *,
    provider_name: str,
    db_path: Path,
    hermes_home: Path,
    profile_name: str,
    lock: ProfileConversationIndexLock,
    stop_event: Optional[threading.Event] = None,
    handle: Optional[ConversationIndexRuntimeHandle] = None,
) -> ConversationIndexConsumerStatus:
    if not lock.try_acquire():
        return _status(provider_name, "standby")

    index = None
    consumer = None
    try:
        index = load_conversation_index(provider_name)
        if index is None:
            return _status(provider_name, "unavailable")
        try:
            if not index.is_available():
                return _status(provider_name, "unavailable")
        except Exception as exc:
            return _status(provider_name, "unavailable", error=type(exc).__name__)

        consumer = _open_consumer(
            provider_name=provider_name,
            index=index,
            db_path=Path(db_path),
            hermes_home=Path(hermes_home),
            profile_name=profile_name,
        )
        if handle is not None:
            handle.consumer = consumer
        if stop_event is None:
            consumer.run_once()
            status = consumer.status()
            consumer.close()
            return status
        consumer.run(stop_event)
        return consumer.status()
    finally:
        if handle is not None:
            handle.consumer = None
        if consumer is None and index is not None:
            try:
                index.shutdown()
            except Exception:
                pass
        lock.release()


def _runtime_main(handle: ConversationIndexRuntimeHandle) -> None:
    lock = ProfileConversationIndexLock(handle.hermes_home, handle.provider_name)
    while not handle.stop_event.is_set():
        status = _bootstrap_once(
            provider_name=handle.provider_name,
            db_path=handle.db_path,
            hermes_home=handle.hermes_home,
            profile_name=handle.profile_name,
            lock=lock,
            stop_event=handle.stop_event,
            handle=handle,
        )
        handle.bootstrap_status = status
        if handle.stop_event.is_set():
            break
        delay = 2.0 if status.state == "standby" else 10.0
        handle.stop_event.wait(delay)


def _start_runtime_thread(
    *, provider_name: str, db_path: Path, hermes_home: Path, profile_name: str,
) -> ConversationIndexRuntimeHandle:
    handle = ConversationIndexRuntimeHandle(
        provider_name=provider_name,
        db_path=Path(db_path),
        hermes_home=Path(hermes_home),
        profile_name=profile_name,
    )
    thread = threading.Thread(
        target=_runtime_main,
        args=(handle,),
        name=f"conversation-index:{profile_name}:{provider_name}",
        daemon=True,
    )
    handle.thread = thread
    thread.start()
    return handle


def ensure_conversation_index_consumer(
    *,
    provider_name: str,
    db_path: Optional[Path] = None,
    hermes_home: Optional[Path] = None,
    profile_name: Optional[str] = None,
) -> Optional[ConversationIndexRuntimeHandle]:
    """Start no worker unless the configured provider has a matching index entry point."""
    clean = str(provider_name or "").strip()
    if not clean or find_conversation_index_entry_point(clean) is None:
        return None

    if hermes_home is None:
        from hermes_constants import get_hermes_home
        hermes_home = get_hermes_home()
    home = Path(hermes_home).resolve()
    db = Path(db_path or (home / "state.db")).resolve()
    if profile_name is None:
        try:
            from hermes_cli.profiles import get_active_profile_name
            profile_name = get_active_profile_name()
        except Exception:
            profile_name = "default"

    key = (str(home), clean)
    with _RUNTIMES_LOCK:
        existing = _RUNTIMES.get(key)
        if existing is not None and existing.thread is not None and existing.thread.is_alive():
            return existing
        handle = _start_runtime_thread(
            provider_name=clean, db_path=db, hermes_home=home, profile_name=profile_name,
        )
        _RUNTIMES[key] = handle
        return handle


def get_conversation_index_status(
    provider_name: str, *, hermes_home: Optional[Path] = None,
) -> Optional[ConversationIndexConsumerStatus]:
    if hermes_home is None:
        from hermes_constants import get_hermes_home
        hermes_home = get_hermes_home()
    key = (str(Path(hermes_home).resolve()), str(provider_name or "").strip())
    with _RUNTIMES_LOCK:
        handle = _RUNTIMES.get(key)
    return handle.status() if handle is not None else None


def stop_all_conversation_index_consumers() -> None:
    with _RUNTIMES_LOCK:
        handles = list(_RUNTIMES.values())
        _RUNTIMES.clear()
    for handle in handles:
        handle.stop()


atexit.register(stop_all_conversation_index_consumers)