from unittest.mock import patch

from agent.conversation_index_search_runtime import search_conversation_index
from conversation_index import ConversationIndex, MessageReference
from hermes_state import SessionDB


class SearchIndex(ConversationIndex):
    def __init__(self, ref=None, *, available=True, explode=False):
        self.ref = ref
        self.available = available
        self.explode = explode
        self.source = None
        self.closed = False

    def initialize(self, source, *, profile_name, hermes_home):
        self.source = source

    def is_available(self):
        return self.available

    def consume_changes(self, changes, *, after_cursor):
        return after_cursor

    def search(self, query, *, conversation_ids, limit):
        if self.explode:
            raise RuntimeError("search failed")
        return () if self.ref is None else (self.ref,)

    def reset_for_rebuild(self, *, snapshot_watermark):
        return None

    def shutdown(self):
        self.closed = True


def test_runtime_search_loads_index_and_returns_core_hydrated_text(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("alpha", source="test")
        row_id = db.append_message("alpha", role="user", content="runtime text")
        entry = db.get_conversation_snapshot().messages[0]
        ref = MessageReference("alpha", row_id, entry.content_hash, 0, entry.text_length, 1.0)
    finally:
        db.close()

    index = SearchIndex(ref)
    with patch("agent.conversation_index_search_runtime.load_conversation_index", return_value=index):
        results = search_conversation_index(
            provider_name="fake", query="runtime", db_path=tmp_path / "state.db",
            hermes_home=tmp_path, profile_name="default",
        )

    assert [item.text for item in results] == ["runtime text"]
    assert index.closed is True


def test_runtime_search_failure_is_nonfatal(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.close()
    index = SearchIndex(explode=True)
    with patch("agent.conversation_index_search_runtime.load_conversation_index", return_value=index):
        assert search_conversation_index(
            provider_name="fake", query="x", db_path=tmp_path / "state.db",
            hermes_home=tmp_path, profile_name="default",
        ) == ()
    assert index.closed is True


def test_runtime_unavailable_index_returns_no_hits_without_opening_db(tmp_path):
    index = SearchIndex(available=False)
    with (
        patch("agent.conversation_index_search_runtime.load_conversation_index", return_value=index),
        patch("agent.conversation_index_search_runtime.ConversationIndexSearchService") as service,
    ):
        assert search_conversation_index(
            provider_name="fake", query="x", db_path=tmp_path / "state.db",
            hermes_home=tmp_path, profile_name="default",
        ) == ()
    service.assert_not_called()
    assert index.closed is True
