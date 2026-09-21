"""Provider-neutral contracts for derived conversation indexing.

Hermes owns canonical transcripts. These value types describe durable change-feed
records and index search references; they deliberately contain no message body.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Optional


class ConversationChangeType(str, Enum):
    """Stable semantic events emitted after canonical transcript mutations."""

    MESSAGE_UPSERT = "message_upsert"
    MESSAGE_STATE = "message_state"
    CONVERSATION_RECONCILE = "conversation_reconcile"
    CONVERSATION_DELETE = "conversation_delete"


class MessageIndexState(str, Enum):
    """Canonical message states that matter to a derived transcript index."""

    ACTIVE = "active"
    COMPACTED = "compacted"
    INACTIVE = "inactive"


@dataclass(frozen=True)
class ConversationChange:
    """One durable, body-free canonical transcript change."""

    sequence: int
    change_type: ConversationChangeType
    conversation_id: str
    created_at: float
    message_id: Optional[int] = None
    content_hash: Optional[str] = None
    state: Optional[MessageIndexState] = None

    def __post_init__(self) -> None:
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int) or self.sequence <= 0:
            raise ValueError("sequence must be a positive integer")
        if not isinstance(self.conversation_id, str) or not self.conversation_id:
            raise ValueError("conversation_id must be non-empty")
        if (
            isinstance(self.created_at, bool)
            or not isinstance(self.created_at, (int, float))
            or not math.isfinite(float(self.created_at))
        ):
            raise ValueError("created_at must be finite")

        if not isinstance(self.change_type, ConversationChangeType):
            raise ValueError("change_type must be a ConversationChangeType")
        message_event = self.change_type in {
            ConversationChangeType.MESSAGE_UPSERT,
            ConversationChangeType.MESSAGE_STATE,
        }
        if message_event:
            if isinstance(self.message_id, bool) or not isinstance(self.message_id, int) or self.message_id <= 0:
                raise ValueError("message events require a positive message_id")
            if not isinstance(self.content_hash, str) or not self.content_hash:
                raise ValueError("message events require a content_hash")
            if not isinstance(self.state, MessageIndexState):
                raise ValueError("message events require a state")
        elif self.message_id is not None or self.content_hash is not None or self.state is not None:
            raise ValueError("conversation-level events cannot carry message fields")


@dataclass(frozen=True)
class MessageReference:
    """Untrusted index hit that Hermes must authorize, validate, and hydrate."""

    conversation_id: str
    message_id: int
    content_hash: str
    start: int
    end: int
    score: float
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.conversation_id, str) or not self.conversation_id:
            raise ValueError("conversation_id must be non-empty")
        if isinstance(self.message_id, bool) or not isinstance(self.message_id, int) or self.message_id <= 0:
            raise ValueError("message_id must be a positive integer")
        if not isinstance(self.content_hash, str) or not self.content_hash:
            raise ValueError("content_hash must be non-empty")
        if (
            isinstance(self.start, bool)
            or isinstance(self.end, bool)
            or not isinstance(self.start, int)
            or not isinstance(self.end, int)
            or self.start < 0
            or self.end < self.start
        ):
            raise ValueError("reference offsets must satisfy 0 <= start <= end")
        if (
            isinstance(self.score, bool)
            or not isinstance(self.score, (int, float))
            or not math.isfinite(float(self.score))
        ):
            raise ValueError("score must be finite")
        if not isinstance(self.metadata, Mapping):
            raise ValueError("metadata must be a mapping")
