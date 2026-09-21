from pathlib import Path

from agent import conversation_index_runtime as runtime
from conversation_index import ConversationIndex
from hermes_state import SessionDB


class FakeIndex(ConversationIndex):
    def is_available(self):
        return True

    def consume_changes(self, changes, *, after_cursor):
        return changes[-1].sequence if changes else after_cursor

    def search(self, query, *, conversation_ids, limit):
        return ()

    def reset_for_rebuild(self, *, snapshot_watermark):
        return None


def test_profile_lock_is_cross_instance_exclusive(tmp_path):
    first = runtime.ProfileConversationIndexLock(tmp_path, "fake")
    second = runtime.ProfileConversationIndexLock(tmp_path, "fake")

    assert first.try_acquire() is True
    try:
        assert second.try_acquire() is False
    finally:
        first.release()

    assert second.try_acquire() is True
    second.release()


def test_no_entry_point_means_no_worker_or_session_db(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime, "find_conversation_index_entry_point", lambda name: None)
    opened = []
    monkeypatch.setattr(runtime, "_start_runtime_thread", lambda *args, **kwargs: opened.append(True))

    assert runtime.ensure_conversation_index_consumer(
        provider_name="missing",
        db_path=tmp_path / "state.db",
        hermes_home=tmp_path,
        profile_name="default",
    ) is None
    assert opened == []


def test_unavailable_index_does_not_open_session_db(monkeypatch, tmp_path):
    class Unavailable(FakeIndex):
        def is_available(self):
            return False

    monkeypatch.setattr(runtime, "load_conversation_index", lambda name: Unavailable())
    opened = []
    monkeypatch.setattr(runtime, "_open_consumer", lambda *args, **kwargs: opened.append(True))

    status = runtime._bootstrap_once(
        provider_name="fake",
        db_path=tmp_path / "state.db",
        hermes_home=tmp_path,
        profile_name="default",
        lock=runtime.ProfileConversationIndexLock(tmp_path, "fake"),
    )

    assert status.state == "unavailable"
    assert opened == []


def test_index_failure_does_not_affect_canonical_append(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("alpha", source="test")
        assert db.append_message("alpha", role="user", content="canonical survives") > 0
        assert db.get_messages("alpha")[0]["content"] == "canonical survives"
    finally:
        db.close()


def test_same_index_can_run_independently_in_two_profile_homes(tmp_path):
    first = runtime.ProfileConversationIndexLock(tmp_path / "one", "fake")
    second = runtime.ProfileConversationIndexLock(tmp_path / "two", "fake")

    assert first.try_acquire() is True
    assert second.try_acquire() is True

    first.release()
    second.release()
