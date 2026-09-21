"""Provider-neutral contracts for derived conversation indexing.

Hermes owns canonical transcripts. These value types describe durable change-feed
records and index search references; they deliberately contain no message body.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Optional, Tuple


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


def canonical_content_hash(storage_type: str, content_bytes: Optional[bytes]) -> str:
    """Hash the exact SQLite storage representation used by the change feed."""
    if not isinstance(storage_type, str) or not storage_type:
        raise ValueError("storage_type must be non-empty")
    payload = b"" if content_bytes is None else bytes(content_bytes)
    digest = hashlib.sha256(storage_type.encode("ascii") + b"\0" + payload).hexdigest()
    return f"sha256:{digest}"


def canonical_message_index_state(active: int, compacted: int) -> MessageIndexState:
    """Map canonical row flags to the stable derived-index state vocabulary."""
    if int(active or 0) == 1:
        return MessageIndexState.ACTIVE
    if int(compacted or 0) == 1:
        return MessageIndexState.COMPACTED
    return MessageIndexState.INACTIVE


def canonical_hydration_text(decoded_content: Any) -> str:
    """Stable text representation whose character offsets are used by MessageReference."""
    if decoded_content is None:
        return ""
    if isinstance(decoded_content, str):
        return decoded_content
    if isinstance(decoded_content, bytes):
        return decoded_content.decode("utf-8", errors="replace")
    try:
        return json.dumps(
            decoded_content, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
        )
    except (TypeError, ValueError):
        return str(decoded_content)


class ConversationFeedGapError(RuntimeError):
    """The requested cursor predates the oldest retained change."""

    def __init__(self, cursor: int, floor_sequence: int, high_water_sequence: int) -> None:
        self.cursor = cursor
        self.floor_sequence = floor_sequence
        self.high_water_sequence = high_water_sequence
        super().__init__(
            f"conversation change cursor {cursor} predates retained floor {floor_sequence}; rebuild required"
        )


@dataclass(frozen=True)
class ConversationFeedBounds:
    floor_sequence: int
    high_water_sequence: int

    def __post_init__(self) -> None:
        if self.high_water_sequence < 0 or self.floor_sequence < 1:
            raise ValueError("invalid conversation feed bounds")
        if self.floor_sequence > self.high_water_sequence + 1:
            raise ValueError("feed floor cannot exceed high-water + 1")


@dataclass(frozen=True)
class SnapshotMessage:
    conversation_id: str
    message_id: int
    content_hash: str
    state: MessageIndexState
    text_length: int
    role: str
    timestamp: float


@dataclass(frozen=True)
class ConversationSnapshot:
    watermark: int
    conversation_ids: Tuple[str, ...]
    messages: Tuple[SnapshotMessage, ...]


@dataclass(frozen=True)
class HydratedMessage:
    reference: "MessageReference"
    state: MessageIndexState
    text: str
    role: str
    timestamp: float


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
