import pytest

from conversation_index import ConversationFeedGapError, MessageIndexState
from hermes_state import SessionDB


@pytest.fixture()
def db(tmp_path):
    value = SessionDB(db_path=tmp_path / "state.db")
    value.create_session("alpha", source="test")
    yield value
    value.close()


def test_feed_bounds_and_paged_changes(db):
    first = db.append_message("alpha", role="user", content="one")
    second = db.append_message("alpha", role="assistant", content="two")

    bounds = db.get_conversation_change_bounds()
    assert bounds.floor_sequence == 1
    assert bounds.high_water_sequence == 2

    page = db.get_conversation_changes(after_sequence=0, limit=1)
    assert len(page) == 1
    assert page[0].sequence == 1
    assert page[0].message_id == first
    assert db.get_conversation_changes(after_sequence=page[0].sequence, limit=10)[0].message_id == second


def test_feed_gap_is_explicit_after_retention(db):
    db.append_message("alpha", role="user", content="one")
    db.append_message("alpha", role="assistant", content="two")
    db._conn.execute("DELETE FROM conversation_changes WHERE sequence = 1")
    db._conn.commit()

    bounds = db.get_conversation_change_bounds()
    assert bounds.floor_sequence == 2
    assert bounds.high_water_sequence == 2
    with pytest.raises(ConversationFeedGapError):
        db.get_conversation_changes(after_sequence=0)


def test_profile_scoped_conversation_enumeration(tmp_path):
    first = SessionDB(db_path=tmp_path / "first.db")
    second = SessionDB(db_path=tmp_path / "second.db")
    try:
        first.create_session("alpha", source="test")
        first.create_session("beta", source="test")
        second.create_session("other-profile", source="test")

        assert first.list_index_conversation_ids(limit=10) == ("alpha", "beta")
        assert second.list_index_conversation_ids(limit=10) == ("other-profile",)
        assert first.list_index_conversation_ids(after_id="alpha", limit=10) == ("beta",)
    finally:
        first.close()
        second.close()


def test_snapshot_is_body_free_and_uses_feed_watermark(db):
    active_id = db.append_message("alpha", role="user", content="active body")
    compacted_id = db.append_message("alpha", role="assistant", content="compacted body")
    inactive_id = db.append_message("alpha", role="assistant", content="inactive body")
    db._conn.execute("UPDATE messages SET active = 0, compacted = 1 WHERE id = ?", (compacted_id,))
    db._conn.execute("UPDATE messages SET active = 0, compacted = 0 WHERE id = ?", (inactive_id,))
    db._conn.commit()

    snapshot = db.get_conversation_snapshot()

    assert snapshot.watermark == 3
    assert snapshot.conversation_ids == ("alpha",)
    assert [entry.message_id for entry in snapshot.messages] == [active_id, compacted_id]
    assert [entry.state for entry in snapshot.messages] == [
        MessageIndexState.ACTIVE, MessageIndexState.COMPACTED,
    ]
    assert all(not hasattr(entry, "text") and not hasattr(entry, "content") for entry in snapshot.messages)
    assert snapshot.messages[0].text_length == len("active body")
    assert snapshot.messages[0].role == "user"
    assert isinstance(snapshot.messages[0].timestamp, float)


def test_snapshot_can_be_narrowed_to_authorized_conversations(db):
    db.create_session("beta", source="test")
    db.append_message("alpha", role="user", content="alpha text")
    db.append_message("beta", role="user", content="beta text")

    snapshot = db.get_conversation_snapshot(conversation_ids=["beta", "forged"])

    assert snapshot.conversation_ids == ("beta",)
    assert {entry.conversation_id for entry in snapshot.messages} == {"beta"}


def test_snapshot_hash_matches_feed_hash(db):
    row_id = db.append_message("alpha", role="user", content={"z": 1, "a": 2})

    change = db.get_conversation_changes(after_sequence=0)[0]
    entry = db.get_conversation_snapshot().messages[0]

    assert change.message_id == row_id
    assert entry.message_id == row_id
    assert change.content_hash == entry.content_hash


def test_snapshot_watermark_and_manifest_share_one_read_transaction(db, tmp_path, monkeypatch):
    first_id = db.append_message("alpha", role="user", content="before snapshot")
    peer = SessionDB(db_path=tmp_path / "state.db")
    original = db._existing_index_conversation_ids
    fired = False

    def _interleave(conn, requested):
        nonlocal fired
        if not fired:
            fired = True
            peer.append_message("alpha", role="assistant", content="during snapshot")
        return original(conn, requested)

    monkeypatch.setattr(db, "_existing_index_conversation_ids", _interleave)
    try:
        snapshot = db.get_conversation_snapshot()
    finally:
        peer.close()

    assert snapshot.watermark == 1
    assert [entry.message_id for entry in snapshot.messages] == [first_id]
    assert db.get_conversation_change_bounds().high_water_sequence == 2


def test_empty_feed_has_stable_zero_high_water(db):
    bounds = db.get_conversation_change_bounds()
    assert bounds.floor_sequence == 1
    assert bounds.high_water_sequence == 0
    assert db.get_conversation_changes(after_sequence=0) == ()

    snapshot = db.get_conversation_snapshot()
    assert snapshot.watermark == 0
    assert snapshot.conversation_ids == ("alpha",)
    assert snapshot.messages == ()
