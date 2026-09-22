import pytest

from hermes_state import SessionDB


@pytest.fixture()
def db(tmp_path):
    value = SessionDB(db_path=tmp_path / "state.db")
    value.create_session("sess-index", source="test")
    yield value
    value.close()


def _changes(db):
    return db._conn.execute(
        "SELECT sequence, change_type, conversation_id, message_id, content_hash, state "
        "FROM conversation_changes ORDER BY sequence"
    ).fetchall()


def _clear_changes(db):
    db._conn.execute("DELETE FROM conversation_changes")
    db._conn.commit()


def test_outbox_schema_is_body_free(db):
    columns = {row[1] for row in db._conn.execute("PRAGMA table_info(conversation_changes)")}
    assert columns == {
        "sequence", "change_type", "conversation_id", "message_id",
        "content_hash", "state", "created_at",
    }


def test_append_commits_message_and_change_together(db):
    message_id = db.append_message("sess-index", role="user", content="hello index")

    rows = _changes(db)
    assert len(rows) == 1
    row = rows[0]
    assert row["change_type"] == "message_upsert"
    assert row["conversation_id"] == "sess-index"
    assert row["message_id"] == message_id
    assert row["state"] == "active"
    assert row["content_hash"]
    assert row["content_hash"] != "hello index"


def test_feed_failure_rolls_back_append(db, monkeypatch):
    def fail_feed(*args, **kwargs):
        raise RuntimeError("feed write failed")

    monkeypatch.setattr(SessionDB, "_record_message_change", fail_feed, raising=False)
    with pytest.raises(RuntimeError, match="feed write failed"):
        db.append_message("sess-index", role="user", content="must roll back")

    assert db._conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
    assert db._conn.execute("SELECT COUNT(*) FROM conversation_changes").fetchone()[0] == 0


def test_rewind_emits_inactive_state_for_each_rewound_row(db):
    db.append_message("sess-index", role="user", content="u0")
    db.append_message("sess-index", role="assistant", content="a0")
    target_id = db.append_message("sess-index", role="user", content="u1")
    tail_id = db.append_message("sess-index", role="assistant", content="a1")
    _clear_changes(db)

    result = db.rewind_to_message("sess-index", target_id)

    assert result["rewound_count"] == 2
    rows = _changes(db)
    assert [(row["change_type"], row["message_id"], row["state"]) for row in rows] == [
        ("message_state", target_id, "inactive"),
        ("message_state", tail_id, "inactive"),
    ]


def test_compaction_emits_one_conversation_reconcile(db):
    db.append_message("sess-index", role="user", content="question")
    db.append_message("sess-index", role="assistant", content="answer")
    _clear_changes(db)

    db.archive_and_compact(
        "sess-index",
        [{"role": "assistant", "content": "summary"}],
    )

    rows = _changes(db)
    assert len(rows) == 1
    assert rows[0]["change_type"] == "conversation_reconcile"
    assert rows[0]["conversation_id"] == "sess-index"
    assert rows[0]["message_id"] is None


def test_clear_messages_reconciles_to_empty(db):
    db.append_message("sess-index", role="user", content="erase me")
    _clear_changes(db)

    db.clear_messages("sess-index")

    rows = _changes(db)
    assert len(rows) == 1
    assert rows[0]["change_type"] == "conversation_reconcile"
    assert rows[0]["conversation_id"] == "sess-index"


def test_delete_session_emits_tombstone(db):
    db.append_message("sess-index", role="user", content="goodbye")
    _clear_changes(db)

    assert db.delete_session("sess-index") is True

    rows = _changes(db)
    assert len(rows) == 1
    assert rows[0]["change_type"] == "conversation_delete"
    assert rows[0]["conversation_id"] == "sess-index"
    assert rows[0]["message_id"] is None
