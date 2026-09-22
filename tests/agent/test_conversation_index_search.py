from pathlib import Path

import pytest

from agent.conversation_index_search import ConversationIndexSearchService
from conversation_index import ConversationIndex, MessageReference
from hermes_state import SessionDB


class FakeIndex(ConversationIndex):
    def __init__(self, references=()):
        self.references = tuple(references)
        self.calls = []
        self.source = None
        self.closed = False

    def initialize(self, source, *, profile_name, hermes_home):
        self.source = source

    def is_available(self):
        return True

    def consume_changes(self, changes, *, after_cursor):
        return after_cursor

    def search(self, query, *, conversation_ids, limit):
        self.calls.append((query, conversation_ids, limit))
        return self.references

    def rebuild_from_snapshot(self, snapshot):
        return snapshot.watermark

    def shutdown(self):
        self.closed = True


@pytest.fixture()
def db(tmp_path):
    value = SessionDB(db_path=tmp_path / "state.db")
    value.create_session("alpha", source="test")
    yield value
    value.close()


def _reference(db, conversation_id, message_id, *, start=0, end=None, score=1.0):
    entry = next(
        item for item in db.get_conversation_snapshot(conversation_ids=[conversation_id]).messages
        if item.message_id == message_id
    )
    return MessageReference(
        conversation_id, message_id, entry.content_hash,
        start, entry.text_length if end is None else end, score,
    )


def _searcher(db, tmp_path, index):
    return ConversationIndexSearchService(
        index=index,
        db_path=Path(db.db_path),
        profile_name="default",
        hermes_home=tmp_path,
    )


def test_valid_reference_is_hydrated_from_canonical_state(db, tmp_path):
    row_id = db.append_message("alpha", role="user", content="canonical source")
    ref = _reference(db, "alpha", row_id, start=10)
    index = FakeIndex([ref])
    searcher = _searcher(db, tmp_path, index)
    try:
        results = searcher.search("source", limit=5)
    finally:
        searcher.close()

    assert [item.text for item in results] == ["source"]
    assert results[0].reference is ref
    assert index.calls == [("source", None, 5)]
    assert not hasattr(index.source, "append_message")


def test_deleted_message_reference_is_dropped(db, tmp_path):
    row_id = db.append_message("alpha", role="user", content="delete me")
    ref = _reference(db, "alpha", row_id)
    db.delete_session("alpha")
    searcher = _searcher(db, tmp_path, FakeIndex([ref]))
    try:
        assert searcher.search("delete") == ()
    finally:
        searcher.close()


def test_rewound_inactive_reference_is_dropped(db, tmp_path):
    db.append_message("alpha", role="user", content="keep")
    db.append_message("alpha", role="assistant", content="answer")
    target_id = db.append_message("alpha", role="user", content="rewind target")
    ref = _reference(db, "alpha", target_id)
    db.rewind_to_message("alpha", target_id)
    searcher = _searcher(db, tmp_path, FakeIndex([ref]))
    try:
        assert searcher.search("rewind") == ()
    finally:
        searcher.close()


def test_edited_message_hash_mismatch_is_dropped(db, tmp_path):
    row_id = db.append_message("alpha", role="user", content="before")
    ref = _reference(db, "alpha", row_id)
    assert db.set_user_message_content("alpha", row_id, "after") == 1
    searcher = _searcher(db, tmp_path, FakeIndex([ref]))
    try:
        assert searcher.search("before") == ()
    finally:
        searcher.close()


def test_invalid_offsets_are_dropped(db, tmp_path):
    row_id = db.append_message("alpha", role="user", content="short")
    ref = _reference(db, "alpha", row_id, end=999)
    searcher = _searcher(db, tmp_path, FakeIndex([ref]))
    try:
        assert searcher.search("short") == ()
    finally:
        searcher.close()


def test_explicit_scope_is_intersected_with_local_profile(db, tmp_path):
    db.create_session("beta", source="test")
    alpha_id = db.append_message("alpha", role="user", content="alpha")
    beta_id = db.append_message("beta", role="user", content="beta")
    alpha_ref = _reference(db, "alpha", alpha_id)
    beta_ref = _reference(db, "beta", beta_id)
    index = FakeIndex([beta_ref, alpha_ref])
    searcher = _searcher(db, tmp_path, index)
    try:
        results = searcher.search("x", conversation_ids=["alpha", "foreign"], limit=5)
    finally:
        searcher.close()

    assert [item.reference.conversation_id for item in results] == ["alpha"]
    assert index.calls == [("x", ("alpha",), 5)]


def test_forged_cross_profile_reference_cannot_hydrate(db, tmp_path):
    other = SessionDB(db_path=tmp_path / "other.db")
    try:
        other.create_session("foreign", source="test")
        row_id = other.append_message("foreign", role="user", content="foreign secret")
        foreign_ref = _reference(other, "foreign", row_id)
    finally:
        other.close()

    searcher = _searcher(db, tmp_path, FakeIndex([foreign_ref]))
    try:
        assert searcher.search("secret") == ()
    finally:
        searcher.close()


def test_mixed_valid_and_stale_results_keep_valid_hits(db, tmp_path):
    valid_id = db.append_message("alpha", role="user", content="valid")
    stale_id = db.append_message("alpha", role="user", content="old")
    valid = _reference(db, "alpha", valid_id)
    stale = _reference(db, "alpha", stale_id)
    assert db.set_user_message_content("alpha", stale_id, "new") == 1

    searcher = _searcher(db, tmp_path, FakeIndex([stale, object(), valid]))
    try:
        results = searcher.search("mixed", limit=5)
    finally:
        searcher.close()

    assert [item.text for item in results] == ["valid"]


def test_plugin_result_count_is_bounded_before_hydration(db, tmp_path):
    row_id = db.append_message("alpha", role="user", content="bounded")
    ref = _reference(db, "alpha", row_id)
    index = FakeIndex([ref] * 20)
    searcher = _searcher(db, tmp_path, index)
    try:
        results = searcher.search("bounded", limit=3)
    finally:
        searcher.close()

    assert len(results) == 3
