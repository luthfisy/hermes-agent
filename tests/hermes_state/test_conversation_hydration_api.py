import pytest

from conversation_index import MessageIndexState, MessageReference
from hermes_state import SessionDB


@pytest.fixture()
def db(tmp_path):
    value = SessionDB(db_path=tmp_path / "state.db")
    value.create_session("alpha", source="test")
    yield value
    value.close()


def test_hydration_validates_hash_state_range_and_scope(db):
    row_id = db.append_message("alpha", role="user", content="hello canonical world")
    entry = db.get_conversation_snapshot().messages[0]
    ref = MessageReference(
        conversation_id="alpha", message_id=row_id, content_hash=entry.content_hash,
        start=6, end=15, score=0.9, metadata={"chunk": 1},
    )

    hydrated = db.hydrate_message_references([ref], authorized_conversation_ids=["alpha"])
    assert len(hydrated) == 1
    assert hydrated[0].text == "canonical"
    assert hydrated[0].reference == ref
    assert hydrated[0].state is MessageIndexState.ACTIVE
    assert hydrated[0].role == "user"
    assert isinstance(hydrated[0].timestamp, float)

    stale = MessageReference("alpha", row_id, "stale", 0, 5, 0.1)
    oversized = MessageReference("alpha", row_id, entry.content_hash, 0, 999, 0.1)
    forged = MessageReference("beta", row_id, entry.content_hash, 0, 5, 0.1)
    assert db.hydrate_message_references([stale, oversized, forged]) == ()
    assert db.hydrate_message_references([ref], authorized_conversation_ids=["beta"]) == ()


def test_malformed_result_does_not_block_valid_reference(db):
    row_id = db.append_message("alpha", role="user", content="still valid")
    entry = db.get_conversation_snapshot().messages[0]
    ref = MessageReference("alpha", row_id, entry.content_hash, 0, entry.text_length, 1.0)

    hydrated = db.hydrate_message_references([object(), ref])

    assert len(hydrated) == 1
    assert hydrated[0].reference == ref
    assert hydrated[0].text == "still valid"


def test_hydration_rejects_inactive_by_default(db):
    row_id = db.append_message("alpha", role="user", content="rewound")
    entry = db.get_conversation_snapshot().messages[0]
    db._conn.execute("UPDATE messages SET active = 0, compacted = 0 WHERE id = ?", (row_id,))
    db._conn.commit()
    ref = MessageReference("alpha", row_id, entry.content_hash, 0, 7, 0.5)

    assert db.hydrate_message_references([ref]) == ()
    hydrated = db.hydrate_message_references([ref], include_inactive=True)
    assert hydrated[0].text == "rewound"
    assert hydrated[0].state is MessageIndexState.INACTIVE


def test_structured_content_has_stable_hydration_text(db):
    row_id = db.append_message("alpha", role="user", content={"b": 2, "a": [1, "x"]})
    entry = db.get_conversation_snapshot().messages[0]
    ref = MessageReference("alpha", row_id, entry.content_hash, 0, entry.text_length, 1.0)

    hydrated = db.hydrate_message_references([ref])
    assert hydrated[0].text == '{"a":[1,"x"],"b":2}'
    assert entry.text_length == len(hydrated[0].text)


def test_hydration_is_bounded(db):
    row_id = db.append_message("alpha", role="user", content="bounded")
    entry = db.get_conversation_snapshot().messages[0]
    ref = MessageReference("alpha", row_id, entry.content_hash, 0, 7, 1.0)

    with pytest.raises(ValueError, match="limit"):
        db.hydrate_message_references([ref, ref], limit=1)
    with pytest.raises(ValueError, match="limit"):
        db.hydrate_message_references([ref], limit=257)


def test_compacted_hydration_is_allowed_by_default(db):
    row_id = db.append_message("alpha", role="assistant", content="archived evidence")
    entry = db.get_conversation_snapshot().messages[0]
    db._conn.execute("UPDATE messages SET active = 0, compacted = 1 WHERE id = ?", (row_id,))
    db._conn.commit()
    ref = MessageReference("alpha", row_id, entry.content_hash, 0, entry.text_length, 1.0)

    hydrated = db.hydrate_message_references([ref])
    assert hydrated[0].state is MessageIndexState.COMPACTED
    assert hydrated[0].text == "archived evidence"
    assert db.hydrate_message_references([ref], include_compacted=False) == ()


def test_source_and_hydration_work_on_read_only_handle(tmp_path):
    path = tmp_path / "readonly.db"
    writer = SessionDB(db_path=path)
    writer.create_session("alpha", source="test")
    row_id = writer.append_message("alpha", role="user", content="read only source")
    entry = writer.get_conversation_snapshot().messages[0]
    writer.close()

    reader = SessionDB(db_path=path, read_only=True)
    try:
        assert reader.get_conversation_change_bounds().high_water_sequence == 1
        assert reader.get_conversation_snapshot().messages[0].message_id == row_id
        ref = MessageReference("alpha", row_id, entry.content_hash, 0, entry.text_length, 1.0)
        assert reader.hydrate_message_references([ref])[0].text == "read only source"
    finally:
        reader.close()
