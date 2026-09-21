from unittest.mock import patch


class RecordingMemoryProvider:
    name = "recording"

    def __init__(self):
        self.init_kwargs = {}

    def is_available(self):
        return True

    def initialize(self, session_id, **kwargs):
        self.init_kwargs = dict(kwargs)

    def get_tool_schemas(self):
        return []

    def shutdown(self):
        pass


def test_configured_memory_package_starts_index_after_canonical_db_exists():
    provider = RecordingMemoryProvider()
    cfg = {"memory": {"provider": "recording"}, "agent": {}}

    with (
        patch("hermes_cli.config.load_config", return_value=cfg),
        patch("hermes_cli.config.load_config_readonly", return_value=cfg),
        patch("plugins.memory.load_memory_provider", return_value=provider),
        patch("agent.conversation_index_runtime.ensure_conversation_index_consumer") as ensure_index,
        patch("agent.model_metadata.get_model_context_length", return_value=204_800),
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        from run_agent import AIAgent

        agent = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=False,
            session_id="sess-index-runtime",
        )
        ensure_index.assert_not_called()

        db = agent._get_session_db_for_recall()

    assert db is not None
    ensure_index.assert_called_once()
    kwargs = ensure_index.call_args.kwargs
    assert kwargs["provider_name"] == "recording"
    assert kwargs["db_path"] == db.db_path
    assert kwargs["hermes_home"] == agent._conversation_index_hermes_home
    assert kwargs["profile_name"] == agent._conversation_index_profile_name

    search_callback = provider.init_kwargs["conversation_index_search"]
    with patch(
        "agent.conversation_index_search_runtime.search_conversation_index",
        return_value=("hydrated",),
    ) as search:
        assert search_callback("needle", conversation_ids=["alpha"], limit=7) == ("hydrated",)
    search.assert_called_once()
    search_kwargs = search.call_args.kwargs
    assert search_kwargs["provider_name"] == "recording"
    assert search_kwargs["db_path"] == db.db_path
    assert search_kwargs["hermes_home"] == agent._conversation_index_hermes_home
    assert search_kwargs["profile_name"] == agent._conversation_index_profile_name
    assert search_kwargs["conversation_ids"] == ["alpha"]
    assert search_kwargs["limit"] == 7
    agent.close()
