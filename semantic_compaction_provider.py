"""Provider-neutral semantic compaction proposal contract."""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence, Tuple


def semantic_compaction_fingerprint(messages: Sequence[Mapping[str, Any]]) -> str:
    """Stable digest of the exact source transcript presented for semantic compaction."""
    payload = json.dumps(
        list(messages),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class SemanticCompactionRequest:
    session_id: str
    profile_name: str
    hermes_home: str
    source_fingerprint: str
    messages: Tuple[Mapping[str, Any], ...]
    current_tokens: Optional[int] = None
    focus_topic: Optional[str] = None
    memory_context: str = ""
    force: bool = False


@dataclass(frozen=True)
class SemanticCompactionProposal:
    source_fingerprint: str
    messages: Tuple[Mapping[str, Any], ...]
    summary_index: int
    summary_has_user_turn: bool = True
    metadata: Mapping[str, Any] = field(default_factory=dict)


class SemanticCompactor(ABC):
    """Optional semantic compactor. Hermes validates and commits every proposal."""

    @abstractmethod
    def is_available(self) -> bool:
        """Return whether semantic proposal generation is currently available."""

    @abstractmethod
    def propose(self, request: SemanticCompactionRequest) -> Optional[SemanticCompactionProposal]:
        """Return a proposal for this immutable source snapshot, or None to use native compaction."""

    def shutdown(self) -> None:
        """Release provider resources. Failure must not affect Hermes' native fallback."""
