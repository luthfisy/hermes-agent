from types import SimpleNamespace

from agent import semantic_compaction as semantic
from semantic_compaction_provider import (
    SemanticCompactionProposal,
    SemanticCompactor,
    semantic_compaction_fingerprint,
)


class FakeCompressor:
    def __init__(self):
        self.finalized = None

    def _finalize_compressed(self, candidate, source, n_messages):
        self.finalized = (candidate, source, n_messages)
        self._last_compression_made_progress = True
        return candidate


class FakeCompactor(SemanticCompactor):
    def __init__(self, proposal_fn, *, available=True):
        self.proposal_fn = proposal_fn
        self.available = available
        self.requests = []
        self.closed = False

    def is_available(self):
        return self.available

    def propose(self, request):
        self.requests.append(request)
        return self.proposal_fn(request)

    def shutdown(self):
        self.closed = True


def _agent():
    return SimpleNamespace(
        _semantic_compactor_provider_name="reliquary",
        _conversation_index_profile_name="default",
        _conversation_index_hermes_home="/tmp/hermes",
        session_id="session-1",
        context_compressor=FakeCompressor(),
    )


def _proposal(request, *, fingerprint=None):
    return SemanticCompactionProposal(
        source_fingerprint=fingerprint or request.source_fingerprint,
        messages=(
            {"role": "user", "content": "semantic summary", "_db_persisted": True},
            {"role": "assistant", "content": "preserved tail"},
        ),
        summary_index=0,
        summary_has_user_turn=True,
    )


def test_valid_semantic_proposal_is_stamped_and_finalized(monkeypatch):
    compactor = FakeCompactor(lambda request: _proposal(request))
    monkeypatch.setattr(semantic, "load_semantic_compactor", lambda name: compactor)
    agent = _agent()
    messages = [
        {"role": "user", "content": "old question", "_db_persisted": True},
        {"role": "assistant", "content": "old answer", "_db_persisted": True},
        {"role": "user", "content": "current task", "_db_persisted": True},
    ]

    result = semantic.try_semantic_compaction(
        agent,
        messages,
        current_tokens=1234,
        focus_topic="schema",
        memory_context="reliquary context",
        force=True,
    )

    assert result is not None
    assert result[0]["content"] == "semantic summary"
    assert result[0]["_compressed_summary"] is True
    assert result[0]["_compressed_summary_has_user_turn"] is True
    assert result[0]["display_kind"] == "hidden"
    assert "_db_persisted" not in result[0]
    assert compactor.requests[0].current_tokens == 1234
    assert compactor.requests[0].focus_topic == "schema"
    assert compactor.requests[0].memory_context == "reliquary context"
    assert compactor.requests[0].force is True
    assert compactor.closed is True
    assert agent.context_compressor.finalized is not None


def test_stale_source_falls_back_to_native(monkeypatch):
    messages = [
        {"role": "user", "content": "before"},
        {"role": "assistant", "content": "answer"},
    ]

    def _mutating_proposal(request):
        messages.append({"role": "user", "content": "late arrival"})
        return _proposal(request)

    compactor = FakeCompactor(_mutating_proposal)
    monkeypatch.setattr(semantic, "load_semantic_compactor", lambda name: compactor)

    assert semantic.try_semantic_compaction(
        _agent(), messages, current_tokens=None, focus_topic=None, memory_context="", force=False,
    ) is None
    assert compactor.closed is True


def test_mismatched_fingerprint_falls_back_to_native(monkeypatch):
    compactor = FakeCompactor(lambda request: _proposal(request, fingerprint="sha256:stale"))
    monkeypatch.setattr(semantic, "load_semantic_compactor", lambda name: compactor)

    assert semantic.try_semantic_compaction(
        _agent(),
        [{"role": "user", "content": "source"}],
        current_tokens=None,
        focus_topic=None,
        memory_context="",
        force=False,
    ) is None


def test_unavailable_or_failed_provider_falls_back_to_native(monkeypatch):
    unavailable = FakeCompactor(lambda request: None, available=False)
    monkeypatch.setattr(semantic, "load_semantic_compactor", lambda name: unavailable)
    assert semantic.try_semantic_compaction(
        _agent(), [{"role": "user", "content": "source"}],
        current_tokens=None, focus_topic=None, memory_context="", force=False,
    ) is None

    failed = FakeCompactor(lambda request: (_ for _ in ()).throw(RuntimeError("offline")))
    monkeypatch.setattr(semantic, "load_semantic_compactor", lambda name: failed)
    assert semantic.try_semantic_compaction(
        _agent(), [{"role": "user", "content": "source"}],
        current_tokens=None, focus_topic=None, memory_context="", force=False,
    ) is None


def test_fingerprint_changes_with_source_content():
    first = [{"role": "user", "content": "one"}]
    second = [{"role": "user", "content": "two"}]
    assert semantic_compaction_fingerprint(first) != semantic_compaction_fingerprint(second)
