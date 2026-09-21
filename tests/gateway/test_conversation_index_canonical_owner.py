"""Phase 6 acceptance: one canonical gateway write fans out to one derived index entry."""

from pathlib import Path

from agent.conversation_index_consumer import ConversationIndexConsumer
from agent.conversation_index_search import ConversationIndexSearchService
from agent.conversation_index_storage import ConversationIndexCursorStore
from conversation_index import MessageReference
from conversation_index_provider import ConversationIndex
from gateway.config import GatewayConfig
from gateway.session import SessionStore


class RecordingIndex(ConversationIndex):
    def __init__(self):
        self.source = None
        self.entries = {}
        self.applied_sequences = []

    def initialize(self, source, *, profile_name, hermes_home):
        self.source = source

    def is_available(self):
        return True

    def consume_changes(self, changes, *, after_cursor):
        for change in changes:
            self.applied_sequences.append(change.sequence)
            if change.message_id is None or change.content_hash is None:
                continue
            snapshot = self.source.get_conversation_snapshot(
                conversation_ids=[change.conversation_id]
            )
            entry = next(
                item for item in snapshot.messages
                if item.message_id == change.message_id
            )
            key = (change.conversation_id, change.message_id)
            self.entries[key] = MessageReference(
                change.conversation_id,
                change.message_id,
                change.content_hash,
                0,
                entry.text_length,
                1.0,
            )
        return changes[-1].sequence if changes else after_cursor

    def search(self, query, *, conversation_ids, limit):
        allowed = None if conversation_ids is None else set(conversation_ids)
        return tuple(
            ref for ref in self.entries.values()
            if allowed is None or ref.conversation_id in allowed
        )[:limit]

    def rebuild_from_snapshot(self, snapshot):
        self.entries.clear()
        for entry in snapshot.messages:
            self.entries[(entry.conversation_id, entry.message_id)] = MessageReference(
                entry.conversation_id,
                entry.message_id,
                entry.content_hash,
                0,
                entry.text_length,
                1.0,
            )
        return snapshot.watermark


def test_canonical_gateway_owner_fans_out_exactly_once(tmp_path, monkeypatch):
    import hermes_state

    db_path = tmp_path / "state.db"
    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", db_path)

    sessions_dir = tmp_path / "sessions"
    owner = SessionStore(sessions_dir=sessions_dir, config=GatewayConfig())
    sid = "phase6-canonical-owner"
    owner._db.create_session(session_id=sid, source="test")

    message = {
        "role": "user",
        "content": "one canonical turn",
        "timestamp": 1.0,
        "platform_message_id": "platform-1",
    }

    # Gateway owner admission writes canonical state. The worker/fallback path is
    # explicitly non-owning and therefore cannot write the same row again.
    owner.append_to_transcript(sid, message)
    owner.append_to_transcript(sid, message, skip_db=True)

    canonical = owner._db.get_messages(sid)
    changes = owner._db.get_conversation_changes(after_sequence=0, limit=10)
    assert [row["content"] for row in canonical] == ["one canonical turn"]
    assert len(changes) == 1
    assert changes[0].conversation_id == sid
    assert changes[0].message_id == canonical[0]["id"]

    # A distinct surface resumes from the same canonical transcript.
    reader = SessionStore(sessions_dir=sessions_dir, config=GatewayConfig())
    resumed = reader.load_transcript(sid)
    assert [row["content"] for row in resumed] == ["one canonical turn"]

    # Nothing reaches the derived index until the async feed consumer runs.
    index = RecordingIndex()
    cursor_store = ConversationIndexCursorStore(tmp_path, "recording")
    consumer = ConversationIndexConsumer(
        index_name="recording",
        index=index,
        db_path=Path(owner._db.db_path),
        cursor_store=cursor_store,
        profile_name="default",
        hermes_home=tmp_path,
        batch_size=10,
    )
    assert index.entries == {}
    assert consumer.run_once() == changes[0].sequence
    assert index.applied_sequences == [changes[0].sequence]
    assert len(index.entries) == 1
    assert cursor_store.load() == changes[0].sequence

    # Replay at the same durable cursor is a no-op: one feed mutation stays one derived entry.
    assert consumer.run_once() == changes[0].sequence
    assert index.applied_sequences == [changes[0].sequence]
    assert len(index.entries) == 1

    # Search returns only a reference; Hermes authorizes and hydrates canonical text.
    search = ConversationIndexSearchService(
        index=index,
        db_path=Path(owner._db.db_path),
        profile_name="default",
        hermes_home=tmp_path,
    )
    try:
        results = search.search("canonical", conversation_ids=[sid], limit=5)
    finally:
        search.close()

    assert [item.text for item in results] == ["one canonical turn"]
    assert len(owner._db.get_conversation_changes(after_sequence=0, limit=10)) == 1
    assert len(owner._db.get_messages(sid)) == 1

    reader._db.close()
    owner._db.close()
