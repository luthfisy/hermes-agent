"""A host that mirrors the turn's profile into HERMES_HOME must not flip routed-profile detection.

Hermes WebUI serves several profiles from one process and, for legacy readers, mirrors the active
turn's profile into ``os.environ["HERMES_HOME"]`` while also installing the context-local override.
Without a pinned process home the override then equals the "process" home, the turn is treated as
the launch profile, and its MCP connections are keyed by bare name — shared with every other profile
that configures a server of the same name.
"""
from __future__ import annotations

import pytest

import hermes_constants
from agent.secret_scope import serves_routed_profile


@pytest.fixture
def homes(tmp_path, monkeypatch):
    launch = tmp_path / "launch"
    served = tmp_path / "profiles" / "served"
    launch.mkdir()
    served.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(launch))
    monkeypatch.setattr(hermes_constants, "_PINNED_PROCESS_HERMES_HOME", None)
    return launch, served


def _with_override(path, fn):
    token = hermes_constants.set_hermes_home_override(path)
    try:
        return fn()
    finally:
        hermes_constants.reset_hermes_home_override(token)


def test_unpinned_behaviour_is_unchanged(homes, monkeypatch):
    launch, served = homes
    assert _with_override(served, serves_routed_profile) is True
    assert _with_override(launch, serves_routed_profile) is False
    # Mirroring the served home into HERMES_HOME makes it the process home (legacy semantics).
    monkeypatch.setenv("HERMES_HOME", str(served))
    assert _with_override(served, serves_routed_profile) is False


def test_pinned_home_survives_a_mirrored_hermes_home(homes, monkeypatch):
    launch, served = homes
    hermes_constants.pin_process_hermes_home(launch)
    monkeypatch.setenv("HERMES_HOME", str(served))  # the host's per-turn mirror
    assert _with_override(served, serves_routed_profile) is True
    assert _with_override(launch, serves_routed_profile) is False
    assert serves_routed_profile() is False  # no override: the process's own profile
    assert hermes_constants.get_routing_process_hermes_home() == launch
    assert hermes_constants.get_process_hermes_home() == served  # process-asset readers untouched


def test_clearing_the_pin_restores_hermes_home_semantics(homes, monkeypatch):
    launch, served = homes
    hermes_constants.pin_process_hermes_home(launch)
    hermes_constants.pin_process_hermes_home(None)
    monkeypatch.setenv("HERMES_HOME", str(served))
    assert hermes_constants.get_routing_process_hermes_home() == served
    assert _with_override(served, serves_routed_profile) is False


def test_mcp_connection_key_is_profile_scoped_under_a_mirrored_home(homes, monkeypatch):
    from tools.mcp_tool_scope import _server_key
    from tools.registry import registry

    launch, served = homes
    hermes_constants.pin_process_hermes_home(launch)
    monkeypatch.setenv("HERMES_HOME", str(served))
    key = _with_override(served, lambda: _server_key("atlassian"))
    assert key == (hermes_constants.hermes_home_key(served), "atlassian")
    assert _with_override(served, registry.current_scope_key) == key[0]
    assert _with_override(launch, lambda: _server_key("atlassian")) == "atlassian"
