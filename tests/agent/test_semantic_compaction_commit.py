import os
from pathlib import Path
from unittest.mock import MagicMock, patch

from semantic_compaction_provider import (
    SemanticCompactionProposal,
    SemanticCompactor,
)


class CommitCompactor(SemanticCompactor):
    def __init__(self):
        self.requests = []
        self.closed = False

    def is_available(self):
        return True

    def propose(self, request):
        self.requests.append(request)
        return SemanticCompactionProposal(
            source_fingerprint=request.source_fingerprint,
            messages=(
                {"role": "user", "content": "Reliquary semantic summary"},
                {"role": "assistant", "content": "preserved semantic tail"},
            ),
            summary_index=0,
            summary_has_user_turn=True,
        )

    def shutdown(self):
        self.closed = True


def test_semantic_proposal_reaches_existing_canonical_compression_commit(tmp_path):
    from hermes_state import SessionDB
    from run_agent import AIAgent

    db = SessionDB(db_path=Path(tmp_path) / "state.db")
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            model="test/model",
            quiet_mode=True,
            session_db=db,
            session_id="semantic-parent",
            skip_context_files=True,
            skip_memory=True,
        )

    agent.compression_in_place = False
    agent._semantic_compactor_provider_name = "reliquary"

    compressor = MagicMock()
    compressor.compress.side_effect = AssertionError("native compressor must not run")
    compressor._finalize_compressed.side_effect = lambda candidate, source, count: candidate
    compressor.compression_count = 1
    compressor.last_prompt_tokens = 0
    compressor.last_completion_tokens = 0
    compressor._last_summary_error = None
    compressor._last_compress_aborted = False
    compressor._last_compression_made_progress = True
    compressor._last_summary_fallback_used = False
    compressor._last_feasibility_skip = False
    agent.context_compressor = compressor

    messages = [
        {"role": "user" if index % 2 == 0 else "assistant", "content": f"turn-{index}-" + ("x" * 500)}
        for index in range(10)
    ]
    compactor = CommitCompactor()

    with patch("agent.semantic_compaction.load_semantic_compactor", return_value=compactor):
        compressed, _ = agent._compress_context(
            messages,
            "system prompt",
            approx_tokens=10_000,
            force=True,
        )

    compressor.compress.assert_not_called()
    assert compactor.requests
    assert compactor.closed is True
    assert compressed[0]["content"] == "Reliquary semantic summary"
    assert compressed[0]["_compressed_summary"] is True

    assert agent.session_id != "semantic-parent"
    child_rows = db.get_messages(agent.session_id)
    child_contents = [row["content"] for row in child_rows]
    assert child_contents[:2] == [
        "Reliquary semantic summary",
        "preserved semantic tail",
    ]
    # Hermes still owns publication and may append a durable parent-tail row that
    # landed after the compression watermark.
    assert len(child_contents) >= 2

    changes = db.get_conversation_changes(after_sequence=0, limit=100)
    child_changes = [change for change in changes if change.conversation_id == agent.session_id]
    assert len(child_changes) == 1

    parent = db.get_session("semantic-parent")
    assert parent["end_reason"] == "compression"
    agent.close()
