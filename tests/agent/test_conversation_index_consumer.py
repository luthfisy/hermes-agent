from pathlib import Path

import pytest

from agent.conversation_index_consumer import (
    ConversationIndexConsumer,
    ConversationIndexCursorStore,
)
from conversation_index import ConversationIndex
from hermes_state import SessionDB


class FakeIndex(ConversationIndex):
    def __init__(self):
        self.calls = []
        self.fail_once = False
        self.source = None

    def initialize(self, source, *, profile_name, hermes_home):
        self.source = source

    def is_available(self):
        return True

    def consume_changes(self, changes, *, after_cursor):
        self.calls.append((after_cursor, tuple(change.sequence for change in changes)))
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("index offline")
        return changes[-1].sequence if changes else after_cursor

    def search(self, query, *, conversation_ids, limit):
        return ()

    def rebuild_from_snapshot(self, snapshot):
        return snapshot.watermark


@pytest.fixture()
def db(tmp_path):
    value = SessionDB(db_path=tmp_path / "state.db")
    value.create_session("alpha", source="test")
    yield value
    value.close()


def _consumer(db, tmp_path, index=None):
    index = index or FakeIndex()
    store = ConversationIndexCursorStore(tmp_path, "fake")
    consumer = ConversationIndexConsumer(
        index_name="fake",
        index=index,
        db_path=Path(db.db_path),
        cursor_store=store,
        profile_name="default",
        hermes_home=tmp_path,
        batch_size=10,
    )
    return consumer, index, store


def test_cursor_advances_only_after_durable_plugin_apply(db, tmp_path):
    db.append_message("alpha", role="user", content="one")
    db.append_message("alpha", role="assistant", content="two")
    consumer, index, store = _consumer(db, tmp_path)

    assert consumer.run_once() == 2
    assert index.calls == [(0, (1, 2))]
    assert store.load() == 2
    assert consumer.status().cursor == 2


def test_failure_keeps_cursor_and_replays_same_batch(db, tmp_path):
    db.append_message("alpha", role="user", content="one")
    consumer, index, store = _consumer(db, tmp_path)
    index.fail_once = True

    assert consumer.run_once() == 0
    assert store.load() == 0
    assert consumer.status().state == "error"

    assert consumer.run_once() == 1
    assert index.calls == [(0, (1,)), (0, (1,))]
    assert store.load() == 1
    assert consumer.status().state in {"idle", "running"}


def test_feed_gap_rebuilds_and_advances_to_snapshot_watermark(db, tmp_path):
    db.append_message("alpha", role="user", content="one")
    db.append_message("alpha", role="assistant", content="two")
    db._conn.execute("DELETE FROM conversation_changes WHERE sequence = 1")
    db._conn.commit()
    consumer, _, store = _consumer(db, tmp_path)

    assert consumer.run_once() == 2
    assert store.load() == 2
    assert consumer.status().state == "idle"


def test_index_receives_read_only_source_facade(db, tmp_path):
    row_id = db.append_message("alpha", role="user", content="source text")
    consumer, index, _ = _consumer(db, tmp_path)

    consumer.run_once()

    snapshot = index.source.get_conversation_snapshot()
    entry = snapshot.messages[0]
    assert entry.message_id == row_id
    assert not hasattr(index.source, "append_message")


def test_unavailable_index_uses_bounded_retry_status(db, tmp_path):
    class UnavailableIndex(FakeIndex):
        def is_available(self):
            return False

    consumer, _, store = _consumer(db, tmp_path, UnavailableIndex())
    before = __import__("time").time()

    assert consumer.run_once() == 0

    status = consumer.status()
    assert store.load() == 0
    assert status.state == "unavailable"
    assert status.failures == 1
    assert status.next_retry_at is not None
    assert status.next_retry_at > before


def test_partial_commit_advances_only_to_provider_committed_prefix(db, tmp_path):
    db.append_message("alpha", role="user", content="one")
    db.append_message("alpha", role="assistant", content="two")

    class PrefixIndex(FakeIndex):
        def consume_changes(self, changes, *, after_cursor):
            self.calls.append((after_cursor, tuple(change.sequence for change in changes)))
            return changes[0].sequence

    consumer, index, store = _consumer(db, tmp_path, PrefixIndex())

    assert consumer.run_once() == 1
    assert store.load() == 1
    assert consumer.run_once() == 2
    assert index.calls == [(0, (1, 2)), (1, (2,))]


def test_new_consumer_resumes_from_durable_cursor(db, tmp_path):
    db.append_message("alpha", role="user", content="one")
    first, _, store = _consumer(db, tmp_path)
    assert first.run_once() == 1
    first.close()

    second_index = FakeIndex()
    second = ConversationIndexConsumer(
        index_name="fake",
        index=second_index,
        db_path=Path(db.db_path),
        cursor_store=store,
        profile_name="default",
        hermes_home=tmp_path,
        batch_size=10,
    )

    assert second.status().cursor == 1
    assert second.run_once() == 1
    assert second_index.calls == []
    second.close()


def test_invalid_provider_cursor_does_not_advance(db, tmp_path):
    db.append_message("alpha", role="user", content="one")

    class InvalidCursorIndex(FakeIndex):
        def consume_changes(self, changes, *, after_cursor):
            return 999

    consumer, _, store = _consumer(db, tmp_path, InvalidCursorIndex())

    assert consumer.run_once() == 0
    assert store.load() == 0
    assert consumer.status().state == "error"


def test_canonical_append_never_calls_index_directly(db, tmp_path):
    consumer, index, _ = _consumer(db, tmp_path)

    db.append_message("alpha", role="user", content="commit first")

    assert index.calls == []
    assert consumer.status().cursor == 0
    assert consumer.run_once() == 1
    assert index.calls == [(0, (1,))]
