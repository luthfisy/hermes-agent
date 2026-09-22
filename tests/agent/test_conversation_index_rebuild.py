from pathlib import Path

import pytest

from agent.conversation_index_consumer import ConversationIndexConsumer
from agent.conversation_index_storage import ConversationIndexCursorStore
from conversation_index import MessageReference
from conversation_index_provider import ConversationIndex, ConversationIndexRebuildRequired
from hermes_state import SessionDB


class RebuildIndex(ConversationIndex):
    def __init__(self):
        self.source = None
        self.rebuilds = []
        self.calls = []
        self.documents = {}
        self.rebuild_callback = None
        self.force_rebuild = False
        self.force_validation_rebuild = False
        self.validate_calls = []
        self.bad_watermark = False

    def initialize(self, source, *, profile_name, hermes_home):
        self.source = source

    def is_available(self):
        return True

    def validate_cursor(self, cursor):
        self.validate_calls.append(cursor)
        if self.force_validation_rebuild:
            self.force_validation_rebuild = False
            raise ConversationIndexRebuildRequired("derived generation missing")

    def consume_changes(self, changes, *, after_cursor):
        if self.force_rebuild:
            self.force_rebuild = False
            raise ConversationIndexRebuildRequired("derived state missing")
        self.calls.append((after_cursor, tuple(change.sequence for change in changes)))
        return changes[-1].sequence if changes else after_cursor

    def search(self, query, *, conversation_ids, limit):
        return ()

    def rebuild_from_snapshot(self, snapshot):
        self.rebuilds.append(snapshot.watermark)
        rebuilt = {}
        messages = list(snapshot.messages)
        for start in range(0, len(messages), 256):
            chunk = messages[start:start + 256]
            refs = [
                MessageReference(entry.conversation_id, entry.message_id, entry.content_hash, 0, entry.text_length, 0.0)
                for entry in chunk
            ]
            hydrated = self.source.hydrate_message_references(refs, limit=len(refs))
            for item in hydrated:
                rebuilt[(item.reference.conversation_id, item.reference.message_id)] = item.text
        if self.rebuild_callback is not None:
            self.rebuild_callback()
        self.documents = rebuilt
        return snapshot.watermark + (1 if self.bad_watermark else 0)


@pytest.fixture()
def db(tmp_path):
    value = SessionDB(db_path=tmp_path / "state.db")
    value.create_session("alpha", source="test")
    yield value
    value.close()


def _consumer(db, tmp_path, index):
    store = ConversationIndexCursorStore(tmp_path, "fake")
    consumer = ConversationIndexConsumer(
        index_name="fake", index=index, db_path=Path(db.db_path), cursor_store=store,
        profile_name="default", hermes_home=tmp_path, batch_size=10,
    )
    return consumer, store


def test_retention_gap_rebuilds_from_canonical_snapshot(db, tmp_path):
    one = db.append_message("alpha", role="user", content="one")
    two = db.append_message("alpha", role="assistant", content="two")
    db._conn.execute("DELETE FROM conversation_changes WHERE sequence = 1")
    db._conn.commit()

    index = RebuildIndex()
    consumer, store = _consumer(db, tmp_path, index)

    assert consumer.run_once() == 2
    assert store.load() == 2
    assert index.rebuilds == [2]
    assert index.documents == {("alpha", one): "one", ("alpha", two): "two"}
    status = consumer.status()
    assert status.state == "idle"
    assert status.rebuild_required is False
    assert status.cursor == 2
    assert status.lag == 0
    assert status.last_recovery_at is not None


def test_rebuild_replays_changes_committed_after_snapshot_watermark(db, tmp_path):
    db.append_message("alpha", role="user", content="one")
    db._conn.execute("DELETE FROM conversation_changes WHERE sequence = 1")
    db._conn.commit()

    index = RebuildIndex()
    index.rebuild_callback = lambda: db.append_message("alpha", role="assistant", content="later")
    consumer, store = _consumer(db, tmp_path, index)

    assert consumer.run_once() == 1
    assert store.load() == 1
    assert consumer.status().lag == 1

    assert consumer.run_once() == 2
    assert store.load() == 2
    assert index.calls == [(1, (2,))]


def test_restart_validation_rebuilds_lost_index_when_feed_is_caught_up(db, tmp_path):
    row_id = db.append_message("alpha", role="user", content="one")
    first_index = RebuildIndex()
    first, store = _consumer(db, tmp_path, first_index)

    assert first.run_once() == 1
    assert store.load() == 1
    first.close()

    wiped_index = RebuildIndex()
    wiped_index.force_validation_rebuild = True
    restarted = ConversationIndexConsumer(
        index_name="fake",
        index=wiped_index,
        db_path=Path(db.db_path),
        cursor_store=store,
        profile_name="default",
        hermes_home=tmp_path,
        batch_size=10,
    )

    assert db.get_conversation_change_bounds().high_water_sequence == 1
    assert restarted.status().cursor == 1
    assert restarted.run_once() == 1
    assert wiped_index.validate_calls == [1]
    assert wiped_index.calls == []
    assert wiped_index.rebuilds == [1]
    assert wiped_index.documents == {("alpha", row_id): "one"}
    assert restarted.status().state == "idle"
    assert restarted.status().last_recovery_at is not None
    restarted.close()


def test_provider_can_request_rebuild_when_derived_state_is_lost(db, tmp_path):
    db.append_message("alpha", role="user", content="one")
    index = RebuildIndex()
    index.force_rebuild = True
    consumer, store = _consumer(db, tmp_path, index)

    assert consumer.run_once() == 1
    assert store.load() == 1
    assert index.rebuilds == [1]
    assert consumer.status().last_recovery_at is not None


def test_rebuild_watermark_must_match_canonical_snapshot(db, tmp_path):
    db.append_message("alpha", role="user", content="one")
    db._conn.execute("DELETE FROM conversation_changes WHERE sequence = 1")
    db._conn.commit()

    index = RebuildIndex()
    index.bad_watermark = True
    consumer, store = _consumer(db, tmp_path, index)

    assert consumer.run_once() == 0
    assert store.load() == 0
    status = consumer.status()
    assert status.state == "rebuild_required"
    assert status.rebuild_required is True
    assert status.last_error == "ValueError"
    assert status.last_error_at is not None


def test_cursor_ahead_of_canonical_state_forces_rebuild(db, tmp_path):
    db.append_message("alpha", role="user", content="one")
    index = RebuildIndex()
    store = ConversationIndexCursorStore(tmp_path, "fake")
    store.save(99)
    consumer = ConversationIndexConsumer(
        index_name="fake",
        index=index,
        db_path=Path(db.db_path),
        cursor_store=store,
        profile_name="default",
        hermes_home=tmp_path,
        batch_size=10,
    )

    assert consumer.run_once() == 1
    assert store.load() == 1
    assert index.rebuilds == [1]
    assert consumer.status().lag == 0


def test_failed_rebuild_remains_sticky_until_success(db, tmp_path):
    db.append_message("alpha", role="user", content="one")
    db._conn.execute("DELETE FROM conversation_changes WHERE sequence = 1")
    db._conn.commit()

    class FlakyRebuildIndex(RebuildIndex):
        def __init__(self):
            super().__init__()
            self.rebuild_attempts = 0

        def rebuild_from_snapshot(self, snapshot):
            self.rebuild_attempts += 1
            if self.rebuild_attempts == 1:
                raise RuntimeError("rebuild failed")
            return super().rebuild_from_snapshot(snapshot)

    index = FlakyRebuildIndex()
    consumer, store = _consumer(db, tmp_path, index)

    assert consumer.run_once() == 0
    assert store.load() == 0
    first = consumer.status()
    assert first.state == "rebuild_required"
    assert first.rebuild_required is True
    assert first.last_error == "RuntimeError"

    assert consumer.run_once() == 1
    assert store.load() == 1
    second = consumer.status()
    assert second.state == "idle"
    assert second.rebuild_required is False
    assert second.last_error == "RuntimeError"
    assert second.last_error_at == first.last_error_at
    assert second.last_recovery_at is not None
