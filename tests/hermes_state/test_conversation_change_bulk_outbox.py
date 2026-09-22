import pytest

from hermes_state import SessionDB


@pytest.fixture()
def db(tmp_path):
    value = SessionDB(db_path=tmp_path / "state.db")
    value.create_session("sess-index", source="test")
    yield value
    value.close()


def _clear(db):
    db._conn.execute("DELETE FROM conversation_changes")
    db._conn.commit()


def _events(db):
    return db._conn.execute(
        "SELECT change_type, conversation_id, message_id, content_hash, state "
        "FROM conversation_changes ORDER BY sequence"
    ).fetchall()


def test_destructive_replace_emits_reconcile(db):
    db.append_message("sess-index", role="user", content="old")
    _clear(db)

    db.replace_messages("sess-index", [{"role": "user", "content": "new"}])

    rows = _events(db)
    assert [(r["change_type"], r["conversation_id"]) for r in rows] == [
        ("conversation_reconcile", "sess-index"),
    ]


def test_archive_replace_emits_state_then_upsert(db):
    db.append_message("sess-index", role="user", content="u0")
    db.append_message("sess-index", role="assistant", content="a0")
    dropped_user = db.append_message("sess-index", role="user", content="u1")
    dropped_assistant = db.append_message("sess-index", role="assistant", content="a1")
    kept = db.get_messages("sess-index")[:2]
    replacement = [*kept, {"role": "user", "content": "u1 edited"}]
    _clear(db)

    db.replace_messages("sess-index", replacement, active_only=True, archive_dropped=True)

    rows = _events(db)
    assert [(r["change_type"], r["message_id"], r["state"]) for r in rows[:2]] == [
        ("message_state", dropped_user, "inactive"),
        ("message_state", dropped_assistant, "inactive"),
    ]
    assert rows[2]["change_type"] == "message_upsert"
    assert rows[2]["message_id"] == replacement[-1]["_row_id"]
    assert rows[2]["state"] == "active"


def test_user_content_rewrite_emits_upsert(db):
    row_id = db.append_message("sess-index", role="user", content="raw")
    old_hash = _events(db)[0]["content_hash"]
    _clear(db)

    assert db.set_user_message_content("sess-index", row_id, "expanded") == 1

    rows = _events(db)
    assert len(rows) == 1
    assert rows[0]["change_type"] == "message_upsert"
    assert rows[0]["message_id"] == row_id
    assert rows[0]["content_hash"] != old_hash


def test_negative_driver_rowcount_uses_sqlite_changes_fallback(db):
    class NegativeCursor:
        rowcount = -1

    class ChangesResult:
        @staticmethod
        def fetchone():
            return (1,)

    class FakeConn:
        @staticmethod
        def execute(sql):
            assert sql == "SELECT changes()"
            return ChangesResult()

    assert db._resolved_rowcount(FakeConn(), NegativeCursor()) == 1


def test_user_content_rewrite_miss_does_not_publish_outbox_event(db):
    _clear(db)

    assert db.set_user_message_content("sess-index", 999_999, "expanded") == 0
    assert _events(db) == []


def test_batch_blank_row_repair_emits_upsert(db):
    row_id = db.append_message("sess-index", role="assistant", content="")
    _clear(db)

    batch = [{"role": "assistant", "content": "repaired", "_row_id": row_id}]
    assert db.append_messages_batch("sess-index", batch) == 0

    rows = _events(db)
    assert len(rows) == 1
    assert rows[0]["change_type"] == "message_upsert"
    assert rows[0]["message_id"] == row_id


def test_import_sessions_emits_reconcile(tmp_path):
    source = SessionDB(db_path=tmp_path / "source.db")
    target = SessionDB(db_path=tmp_path / "target.db")
    try:
        source.create_session("portable", source="test")
        source.append_message("portable", role="user", content="portable body")
        exported = source.export_session("portable")

        result = target.import_sessions([exported])

        assert result["ok"] is True
        rows = _events(target)
        assert [(r["change_type"], r["conversation_id"]) for r in rows] == [
            ("conversation_reconcile", "portable"),
        ]
    finally:
        source.close()
        target.close()


def test_profile_move_emits_destination_reconcile_and_source_delete(tmp_path):
    source = SessionDB(db_path=tmp_path / "source.db")
    target = SessionDB(db_path=tmp_path / "target.db")
    try:
        source.create_session("move-me", source="test")
        source.append_message("move-me", role="user", content="move body")
        payload = source.export_session_for_move("move-me")
        _clear(source)

        assert target.import_moved_session(payload, profile_name="target") == "imported"
        assert [(r["change_type"], r["conversation_id"]) for r in _events(target)] == [
            ("conversation_reconcile", "move-me"),
        ]

        assert source.delete_moved_session("move-me") is True
        assert [(r["change_type"], r["conversation_id"]) for r in _events(source)] == [
            ("conversation_delete", "move-me"),
        ]
    finally:
        source.close()
        target.close()


def test_bulk_delete_emits_each_tombstone(db):
    db.create_session("delete-a", source="test")
    db.create_session("delete-b", source="test")
    _clear(db)

    assert db.delete_sessions(["delete-a", "delete-b"]) == 2

    rows = _events(db)
    assert {(r["change_type"], r["conversation_id"]) for r in rows} == {
        ("conversation_delete", "delete-a"),
        ("conversation_delete", "delete-b"),
    }


def test_rotating_compression_reconciles_child(db):
    db.append_message("sess-index", role="user", content="before")
    _clear(db)

    db.publish_compression_child(
        parent_session_id="sess-index",
        child_session_id="sess-index-child",
        source="test",
        messages=[{"role": "assistant", "content": "summary"}],
        require_compression_lease=False,
    )

    rows = _events(db)
    assert [(r["change_type"], r["conversation_id"]) for r in rows] == [
        ("conversation_reconcile", "sess-index-child"),
    ]

def test_prune_sessions_emits_tombstone(db):
    db.create_session("prune-me", source="test")
    db._conn.execute(
        "UPDATE sessions SET started_at = 1.0, ended_at = 2.0, end_reason = 'done' WHERE id = ?",
        ("prune-me",),
    )
    db._conn.commit()
    _clear(db)

    assert db.prune_sessions(older_than_days=1, source="test") == 1

    assert [(r["change_type"], r["conversation_id"]) for r in _events(db)] == [
        ("conversation_delete", "prune-me"),
    ]


def test_delete_empty_sessions_emits_tombstone(db):
    db.create_session("empty-ended", source="test")
    db.end_session("empty-ended", "done")
    _clear(db)

    assert db.delete_empty_sessions() == 1

    assert [(r["change_type"], r["conversation_id"]) for r in _events(db)] == [
        ("conversation_delete", "empty-ended"),
    ]
