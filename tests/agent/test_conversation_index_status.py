from pathlib import Path
from unittest.mock import patch

from agent import conversation_index_runtime as runtime
from agent.conversation_index_consumer import ConversationIndexConsumer, ConversationIndexConsumerStatus
from agent.conversation_index_storage import ConversationIndexCursorStore
from conversation_index_provider import ConversationIndex
from hermes_state import SessionDB


class StatusIndex(ConversationIndex):
    def initialize(self, source, *, profile_name, hermes_home):
        self.source = source

    def is_available(self):
        return True

    def consume_changes(self, changes, *, after_cursor):
        return changes[0].sequence if changes else after_cursor

    def search(self, query, *, conversation_ids, limit):
        return ()

    def rebuild_from_snapshot(self, snapshot):
        return snapshot.watermark


def _consumer(tmp_path, db, index):
    return ConversationIndexConsumer(
        index_name="fake",
        index=index,
        db_path=Path(db.db_path),
        cursor_store=ConversationIndexCursorStore(tmp_path, "fake"),
        profile_name="default",
        hermes_home=tmp_path,
        batch_size=10,
    )


def test_runtime_status_preserves_bootstrap_unavailable_state(tmp_path):
    handle = runtime.ConversationIndexRuntimeHandle(
        provider_name="fake",
        db_path=tmp_path / "state.db",
        hermes_home=tmp_path,
        profile_name="default",
    )
    handle.bootstrap_status = ConversationIndexConsumerStatus(
        index_name="fake",
        state="unavailable",
        configured=True,
        available=False,
        last_error="RuntimeError",
    )
    runtime._RUNTIMES[(str(tmp_path.resolve()), "fake")] = handle
    try:
        status = runtime.get_conversation_index_status("fake", hermes_home=tmp_path)
    finally:
        runtime._RUNTIMES.clear()

    assert status is not None
    assert status.configured is True
    assert status.available is False
    assert status.state == "unavailable"


def test_runtime_status_returns_none_when_index_not_started(tmp_path):
    runtime._RUNTIMES.clear()
    with patch("hermes_constants.get_hermes_home", return_value=tmp_path):
        assert runtime.get_conversation_index_status("missing") is None


def test_status_reports_feed_bounds_cursor_lag_and_availability(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("alpha", source="test")
        db.append_message("alpha", role="user", content="one")
        db.append_message("alpha", role="assistant", content="two")
        consumer = _consumer(tmp_path, db, StatusIndex())
        assert consumer.run_once() == 1
        status = consumer.status()
    finally:
        db.close()

    assert status.configured is True
    assert status.available is True
    assert status.feed_floor == 1
    assert status.feed_high_water == 2
    assert status.cursor == 1
    assert status.lag == 1
    assert status.rebuild_required is False


def test_unavailable_status_does_not_expose_content(tmp_path):
    class UnavailableIndex(StatusIndex):
        def is_available(self):
            return False

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("alpha", source="test")
        db.append_message("alpha", role="user", content="secret body")
        consumer = _consumer(tmp_path, db, UnavailableIndex())
        consumer.run_once()
        status = consumer.status()
    finally:
        db.close()

    assert status.available is False
    assert status.state == "unavailable"
    assert "secret body" not in repr(status)
