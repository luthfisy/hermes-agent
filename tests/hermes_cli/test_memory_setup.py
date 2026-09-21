from types import SimpleNamespace
from unittest.mock import MagicMock

import hermes_cli.memory_setup as memory_setup
from hermes_cli.memory_setup import _CANCELLED, _curses_select








def test_cmd_setup_generic_choice_cancel_writes_nothing(tmp_path, monkeypatch):
    class ChoiceProvider:
        def __init__(self):
            self.save_config = MagicMock()

        def get_config_schema(self):
            return [{
                "key": "mode",
                "description": "Mode",
                "default": "one",
                "choices": ["one", "two"],
            }]

    provider = ChoiceProvider()
    selections = iter([0, _CANCELLED])
    save_config = MagicMock()
    install_dependencies = MagicMock()

    monkeypatch.setattr(memory_setup, "_get_available_providers", lambda: [("fake", "local", provider)])
    monkeypatch.setattr(memory_setup, "_curses_select", lambda *args, **kwargs: next(selections))
    monkeypatch.setattr(memory_setup, "_install_dependencies", install_dependencies)
    monkeypatch.setattr(memory_setup, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"memory": {}})
    monkeypatch.setattr("hermes_cli.config.save_config", save_config)

    memory_setup.cmd_setup(SimpleNamespace())

    install_dependencies.assert_called_once_with("fake")
    save_config.assert_not_called()
    provider.save_config.assert_not_called()
    assert not (tmp_path / ".env").exists()


# _write_env_vars's CR/LF-stripping, denylist, and plain-value-roundtrip
# behavior is covered by tests/hermes_cli/test_memory_setup_env_denylist.py,
# which exercises the current save_env_value-routed signature
# (env_writes, hermes_home=None) \u2014 these three tests pinned the prior direct
# Path.write_text(env_path, env_writes) signature/implementation and were
# removed along with it (#60587).


# ---------------------------------------------------------------------------
# _provider_pip_dependencies — mode-aware dep expansion (#70636)
# ---------------------------------------------------------------------------





def test_install_dependencies_force_reinstalls_versioned_specs(tmp_path, monkeypatch):
    """force=True hands every declared spec (version ranges intact) to pip,
    so a downgraded/stripped bridge package is restored on hermes update."""
    import yaml as _yaml

    plugin_dir = tmp_path / "mem0"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.yaml").write_text(
        _yaml.safe_dump({"pip_dependencies": ["mem0ai>=2.0.10,<3"]}), encoding="utf-8"
    )
    monkeypatch.setattr(
        "plugins.memory.find_provider_dir", lambda name: plugin_dir
    )

    installed = []

    def fake_install_specs(specs, timeout=120):
        installed.append(list(specs))
        return SimpleNamespace(ok=True, blocked=False, reason="", stderr="")

    monkeypatch.setattr("tools.lazy_deps.install_specs", fake_install_specs)

    memory_setup._install_dependencies("mem0", force=True)

    assert installed, "force=True must reach the install step"
    assert any("mem0ai>=2.0.10,<3" in specs for specs in installed)


def test_cmd_status_memory_tool_gate_disabled(capsys, monkeypatch):
    """When both memory stores are disabled, Memory status reports memory tool as disabled."""
    _cfg = {"memory": {"memory_enabled": False, "user_profile_enabled": False}}
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: _cfg)
    # check_memory_requirements() reads the readonly loader, not load_config.
    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly", lambda: _cfg, raising=False
    )
    monkeypatch.setattr(memory_setup, "_get_available_providers", lambda: [])

    memory_setup.cmd_status(SimpleNamespace())

    captured = capsys.readouterr().out
    assert "Memory tool:        disabled ✗" in captured
    assert "Memory injection:   disabled ✗" in captured
    assert "User profile:       disabled ✗" in captured


def test_cmd_status_memory_tool_gate_enabled(capsys, monkeypatch):
    """When at least one memory store is enabled, Memory status reports memory tool as enabled."""
    _cfg = {"memory": {"memory_enabled": True, "user_profile_enabled": False}}
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: _cfg)
    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly", lambda: _cfg, raising=False
    )
    monkeypatch.setattr(memory_setup, "_get_available_providers", lambda: [])

    memory_setup.cmd_status(SimpleNamespace())

    captured = capsys.readouterr().out
    assert "Memory tool:        enabled ✓" in captured
    assert "Memory injection:   enabled ✓" in captured
    assert "User profile:       disabled ✗" in captured


# ---------------------------------------------------------------------------
# provider answers reach a writer — memory.<name> when the plugin overrides nothing
# ---------------------------------------------------------------------------


def _real_provider(schema, *, own_writer):
    """A provider built on the real ABC, because that is what every bundled plugin is:
    MemoryProvider.save_config exists with an EMPTY body, so a plain hasattr() check cannot
    tell a plugin that persists from one that silently drops."""
    from agent.memory_provider import MemoryProvider

    class _Provider(MemoryProvider):
        name = "test-provider"

        def initialize(self, session_id, **kwargs):
            pass

        def is_available(self):
            return True

        def get_tool_schemas(self):
            return []

        def get_config_schema(self):
            return schema

    if own_writer:
        _Provider.save_config = MagicMock()
    return _Provider()


def _setup_harness(monkeypatch, tmp_path, provider, name, selections, answer, saved):
    """Drive cmd_setup with a scripted picker/prompt, deep-copying what save_config receives so a
    mutation made after the save cannot masquerade as a persisted value."""
    from copy import deepcopy

    picks = iter(selections)
    monkeypatch.setattr(memory_setup, "_get_available_providers", lambda: [(name, "local", provider)])
    monkeypatch.setattr(memory_setup, "_curses_select", lambda *a, **k: next(picks))
    monkeypatch.setattr(memory_setup, "_install_dependencies", MagicMock())
    monkeypatch.setattr(memory_setup, "_prompt", lambda *a, **k: answer)
    monkeypatch.setattr(memory_setup, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"memory": {}})
    monkeypatch.setattr("hermes_cli.config.save_config", lambda cfg, **kw: saved.update(deepcopy(cfg)))


def test_setup_persists_answers_when_the_plugin_does_not_override_save_config(tmp_path, monkeypatch):
    """The retaindb/byterover shape: subclasses MemoryProvider but overrides no writer, so the
    inherited no-op would swallow the values. They belong in memory.<name>, which is where those
    providers read them (plugins/memory/retaindb:41, plugins/memory/byterover:41)."""
    provider = _real_provider(
        [
            {"key": "project", "description": "Project identifier", "default": ""},
            {"key": "mode", "description": "Mode", "default": "one", "choices": ["one", "two"]},
        ],
        own_writer=False,
    )
    saved = {}
    _setup_harness(monkeypatch, tmp_path, provider, "brv", [0, 1], "team-alpha", saved)

    memory_setup.cmd_setup(SimpleNamespace())

    assert saved["memory"]["provider"] == "brv"
    assert saved["memory"].get("brv") == {"project": "team-alpha", "mode": "two"}


def test_setup_does_not_give_a_self_writing_provider_a_second_config_home(tmp_path, monkeypatch):
    """A plugin that really overrides save_config (mem0.json, honcho.json, ...) keeps exactly one
    source of truth: its own writer is used and memory.<name> is left alone."""
    provider = _real_provider([{"key": "host", "description": "Host", "default": ""}], own_writer=True)
    saved = {}
    _setup_harness(monkeypatch, tmp_path, provider, "mem", [0], "https://example.invalid", saved)

    memory_setup.cmd_setup(SimpleNamespace())

    type(provider).save_config.assert_called_once()
    assert type(provider).save_config.call_args[0][0] == {"host": "https://example.invalid"}
    assert "mem" not in saved["memory"]
