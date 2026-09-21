from dataclasses import fields

import pytest

from conversation_index import (
    ConversationChange,
    ConversationChangeType,
    MessageIndexState,
    MessageReference,
)


def test_change_event_vocabulary_is_small_and_stable():
    assert {kind.value for kind in ConversationChangeType} == {
        "message_upsert",
        "message_state",
        "conversation_reconcile",
        "conversation_delete",
    }
    assert {state.value for state in MessageIndexState} == {
        "active",
        "compacted",
        "inactive",
    }


def test_message_change_requires_reference_hash_and_state():
    change = ConversationChange(
        sequence=7,
        change_type=ConversationChangeType.MESSAGE_UPSERT,
        conversation_id="session-1",
        message_id=42,
        content_hash="opaque-hash",
        state=MessageIndexState.ACTIVE,
        created_at=123.5,
    )
    assert change.message_id == 42

    with pytest.raises(ValueError, match="change_type"):
        ConversationChange(
            sequence=8,
            change_type="message_upsert",
            conversation_id="session-1",
            message_id=42,
            content_hash="opaque-hash",
            state=MessageIndexState.ACTIVE,
            created_at=124.0,
        )

    with pytest.raises(ValueError, match="message_id"):
        ConversationChange(
            sequence=8,
            change_type=ConversationChangeType.MESSAGE_STATE,
            conversation_id="session-1",
            content_hash="opaque-hash",
            state=MessageIndexState.INACTIVE,
            created_at=124.0,
        )


def test_conversation_change_cannot_carry_body_or_message_fields():
    assert {field.name for field in fields(ConversationChange)} == {
        "sequence",
        "change_type",
        "conversation_id",
        "created_at",
        "message_id",
        "content_hash",
        "state",
    }

    with pytest.raises(ValueError, match="conversation-level"):
        ConversationChange(
            sequence=9,
            change_type=ConversationChangeType.CONVERSATION_RECONCILE,
            conversation_id="session-1",
            message_id=42,
            content_hash="opaque-hash",
            state=MessageIndexState.ACTIVE,
            created_at=125.0,
        )


def test_message_reference_is_body_free_and_bounds_offsets():
    ref = MessageReference(
        conversation_id="session-1",
        message_id=42,
        content_hash="opaque-hash",
        start=3,
        end=11,
        score=0.75,
        metadata={"provider": "fake"},
    )
    assert not hasattr(ref, "content")
    assert not hasattr(ref, "text")
    assert ref.end == 11

    with pytest.raises(ValueError, match="offsets"):
        MessageReference(
            conversation_id="session-1",
            message_id=42,
            content_hash="opaque-hash",
            start=12,
            end=11,
            score=0.75,
        )


@pytest.mark.parametrize("change_type", [
    ConversationChangeType.CONVERSATION_RECONCILE,
    ConversationChangeType.CONVERSATION_DELETE,
])
def test_conversation_level_changes_have_no_message_identity(change_type):
    change = ConversationChange(
        sequence=10,
        change_type=change_type,
        conversation_id="session-1",
        created_at=126.0,
    )
    assert change.message_id is None
    assert change.content_hash is None
    assert change.state is None
