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

_VALID_PROPOSAL_ROLES = frozenset({"system", "user", "assistant", "tool", "function", "developer"})


def _valid_content_shape(content: Any) -> bool:
    if content is None or isinstance(content, str):
        return True
    if not isinstance(content, list):
        return False
    return all(
        isinstance(block, Mapping)
        and isinstance(block.get("type"), str)
        and bool(block.get("type"))
        for block in content
    )


def _valid_tool_call_shape(tool_call: Any) -> bool:
    if not isinstance(tool_call, Mapping):
        return False
    call_id = tool_call.get("id") or tool_call.get("call_id")
    if not isinstance(call_id, str) or not call_id.strip():
        return False
    call_type = tool_call.get("type", "function")
    if call_type != "function":
        return False
    function = tool_call.get("function")
    if not isinstance(function, Mapping):
        return False
    name = function.get("name")
    arguments = function.get("arguments")
    return (
        isinstance(name, str)
        and bool(name.strip())
        and isinstance(arguments, str)
    )


def _proposal_messages_are_provider_safe(messages: tuple[Any, ...]) -> bool:
    """Reject any candidate Hermes would need to repair before provider rendering."""
    normalized = []
    for raw in messages:
        if not isinstance(raw, Mapping):
            return False
        message = dict(raw)
        role = message.get("role")
        if role not in _VALID_PROPOSAL_ROLES:
            return False
        if not _valid_content_shape(message.get("content")):
            return False

        tool_calls = message.get("tool_calls")
        if tool_calls is not None:
            if (
                role != "assistant"
                or not isinstance(tool_calls, list)
                or not tool_calls
                or not all(_valid_tool_call_shape(call) for call in tool_calls)
            ):
                return False

        if role == "tool":
            tool_call_id = message.get("tool_call_id")
            if not isinstance(tool_call_id, str) or not tool_call_id.strip():
                return False

        if role in {"user", "system", "developer", "function", "tool"} and message.get("content") is None:
            return False
        if role == "assistant" and message.get("content") is None and not tool_calls:
            return False
        normalized.append(message)

    # The pre-provider sanitizer is Hermes' final repair boundary. Run it only on a
    # deep copy: if it would have to alter the proposed transcript, reject the
    # proposal rather than making repaired plugin output canonical.
    from agent.agent_runtime_helpers import sanitize_api_messages

    original = copy.deepcopy(normalized)
    repaired = sanitize_api_messages(copy.deepcopy(normalized))
    return repaired == original


def _valid_proposal(proposal: Any, source_fingerprint: str) -> bool:
    if not isinstance(proposal, SemanticCompactionProposal):
        return False
    if proposal.source_fingerprint != source_fingerprint or not proposal.messages:
        return False
    if isinstance(proposal.summary_index, bool) or not isinstance(proposal.summary_index, int):
        return False
    if not 0 <= proposal.summary_index < len(proposal.messages):
        return False
    if not _proposal_messages_are_provider_safe(proposal.messages):
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
