"""Hermes-side validation and normalization of semantic compaction proposals."""

from __future__ import annotations

import copy
import logging
from collections.abc import Mapping
from typing import Any, Optional

from plugins.semantic_compaction import load_semantic_compactor
from semantic_compaction_provider import (
    SemanticCompactionProposal,
    SemanticCompactionRequest,
    semantic_compaction_fingerprint,
)

logger = logging.getLogger(__name__)


def _valid_proposal(proposal: Any, source_fingerprint: str) -> bool:
    if not isinstance(proposal, SemanticCompactionProposal):
        return False
    if proposal.source_fingerprint != source_fingerprint or not proposal.messages:
        return False
    if isinstance(proposal.summary_index, bool) or not isinstance(proposal.summary_index, int):
        return False
    if not 0 <= proposal.summary_index < len(proposal.messages):
        return False
    if not all(isinstance(message, Mapping) for message in proposal.messages):
        return False
    summary = proposal.messages[proposal.summary_index]
    return summary.get("role") in {"user", "assistant"} and bool(summary.get("content"))


def _normalize_candidate(agent: Any, proposal: SemanticCompactionProposal, source_messages: list) -> Optional[list]:
    candidate = copy.deepcopy([dict(message) for message in proposal.messages])
    summary = candidate[proposal.summary_index]
    from agent.context_compressor import (
        COMPRESSED_SUMMARY_HAS_USER_TURN_KEY,
        COMPRESSED_SUMMARY_METADATA_KEY,
        _DB_PERSISTED_MARKER,
    )

    for message in candidate:
        message.pop(_DB_PERSISTED_MARKER, None)
        message.pop(COMPRESSED_SUMMARY_METADATA_KEY, None)
        message.pop(COMPRESSED_SUMMARY_HAS_USER_TURN_KEY, None)
        message.pop("_row_id", None)
    summary[COMPRESSED_SUMMARY_METADATA_KEY] = True
    summary[COMPRESSED_SUMMARY_HAS_USER_TURN_KEY] = bool(proposal.summary_has_user_turn)
    summary["display_kind"] = "hidden"

    compressor = getattr(agent, "context_compressor", None)
    finalizer = getattr(compressor, "_finalize_compressed", None)
    if not callable(finalizer):
        return None
    compressor._summary_has_user_turn = bool(proposal.summary_has_user_turn)
    compressor._last_compress_aborted = False
    compressor._last_summary_fallback_used = False
    compressor._last_feasibility_skip = False
    return finalizer(candidate, source_messages, len(source_messages))


def try_semantic_compaction(
    agent: Any,
    messages: list,
    *,
    current_tokens: Optional[int],
    focus_topic: Optional[str],
    memory_context: str,
    force: bool,
) -> Optional[list]:
    """Return a validated Hermes-normalized candidate, or None for native fallback."""
    provider_name = getattr(agent, "_semantic_compactor_provider_name", "")
    provider_name = provider_name.strip() if isinstance(provider_name, str) else ""
    if not provider_name:
        return None
    compactor = load_semantic_compactor(provider_name)
    if compactor is None:
        return None
    try:
        if not compactor.is_available():
            return None
        source_copy = copy.deepcopy(messages)
        source_fingerprint = semantic_compaction_fingerprint(source_copy)
        request = SemanticCompactionRequest(
            session_id=str(getattr(agent, "session_id", "") or ""),
            profile_name=str(getattr(agent, "_conversation_index_profile_name", "") or "default"),
            hermes_home=str(getattr(agent, "_conversation_index_hermes_home", "") or ""),
            source_fingerprint=source_fingerprint,
            messages=tuple(source_copy),
            current_tokens=current_tokens,
            focus_topic=focus_topic,
            memory_context=memory_context or "",
            force=bool(force),
        )
        proposal = compactor.propose(request)
        if semantic_compaction_fingerprint(messages) != source_fingerprint:
            logger.warning("Semantic compaction source changed during proposal; using native fallback")
            return None
        if not _valid_proposal(proposal, source_fingerprint):
            return None
        return _normalize_candidate(agent, proposal, messages)
    except Exception:
        logger.warning("Semantic compaction proposal failed; using native fallback", exc_info=True)
        return None
    finally:
        try:
            compactor.shutdown()
        except Exception:
            pass
