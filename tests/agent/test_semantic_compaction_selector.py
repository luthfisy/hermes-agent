from types import SimpleNamespace

from agent import conversation_compression
from agent import semantic_compaction as semantic
from semantic_compaction_provider import SemanticCompactionProposal


def test_selector_uses_semantic_candidate_without_native_dispatch(monkeypatch):
    semantic_candidate = [{"role": "user", "content": "semantic"}]
    monkeypatch.setattr(
        semantic,
        "try_semantic_compaction",
        lambda *args, **kwargs: semantic_candidate,
    )
    monkeypatch.setattr(
        conversation_compression,
        "_resolve_compress_call",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("native should not run")),
    )
    agent = SimpleNamespace(
        context_compressor=SimpleNamespace(),
        _semantic_compactor_provider_name="reliquary",
    )

    result = conversation_compression._select_compression_candidate(
        agent,
        [{"role": "user", "content": "source"}],
        approx_tokens=100,
        focus_topic=None,
        force=False,
        memory_context="",
        bypass_cooldown=False,
        commit_fence=None,
        attempt_generation=1,
        hard_cancel_event=None,
    )

    assert result is semantic_candidate
    assert agent.context_compressor._compression_working_attempt_generation == 1


def test_selector_falls_back_to_native_when_no_semantic_candidate(monkeypatch):
    monkeypatch.setattr(semantic, "try_semantic_compaction", lambda *args, **kwargs: None)
    marker = object()
    monkeypatch.setattr(
        conversation_compression,
        "_resolve_compress_call",
        lambda *args, **kwargs: (marker, {"x": 1}),
    )
    monkeypatch.setattr(
        conversation_compression,
        "_run_summary_dispatch",
        lambda agent, messages, compress_fn, compress_kwargs, **kwargs: ["native"],
    )

    result = conversation_compression._select_compression_candidate(
        SimpleNamespace(
            context_compressor=SimpleNamespace(),
            _semantic_compactor_provider_name="reliquary",
        ),
        [{"role": "user", "content": "source"}],
        approx_tokens=100,
        focus_topic=None,
        force=False,
        memory_context="",
        bypass_cooldown=False,
        commit_fence=None,
        attempt_generation=1,
        hard_cancel_event=None,
    )

    assert result == ["native"]


def test_selector_rejects_malformed_semantic_tail_and_runs_native(monkeypatch):
    class BadCompactor:
        def is_available(self):
            return True

        def propose(self, request):
            return SemanticCompactionProposal(
                source_fingerprint=request.source_fingerprint,
                messages=(
                    {"role": "user", "content": "valid semantic summary"},
                    {"role": "not-a-provider-role", "content": "poison"},
                ),
                summary_index=0,
                summary_has_user_turn=True,
            )

        def shutdown(self):
            return None

    monkeypatch.setattr(semantic, "load_semantic_compactor", lambda name: BadCompactor())
    monkeypatch.setattr(
        conversation_compression,
        "_run_summary_dispatch",
        lambda agent, messages, compress_fn, compress_kwargs, **kwargs: compress_fn(
            messages, **compress_kwargs
        ),
    )
    monkeypatch.setattr(
        conversation_compression,
        "_resolve_compress_call",
        lambda *args, **kwargs: (lambda messages, **_kwargs: ["native"], {}),
    )
    source = [
        {"role": "user", "content": "source question"},
        {"role": "assistant", "content": "source answer"},
    ]
    original = [dict(message) for message in source]
    agent = SimpleNamespace(
        context_compressor=SimpleNamespace(),
        _semantic_compactor_provider_name="reliquary",
        _conversation_index_profile_name="default",
        _conversation_index_hermes_home="/tmp/hermes",
        session_id="session-1",
    )

    result = conversation_compression._select_compression_candidate(
        agent,
        source,
        approx_tokens=100,
        focus_topic=None,
        force=False,
        memory_context="",
        bypass_cooldown=False,
        commit_fence=None,
        attempt_generation=1,
        hard_cancel_event=None,
    )

    assert result == ["native"]
    assert source == original
