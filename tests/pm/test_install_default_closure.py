"""The default `pm install` closure must stage the interpreter boot requires.

A fresh source install emits boot launchers (the source completion's
publish_launchers, hermes_cli/_launchers.py) that exec the pm STORE
interpreter. The `python`
package is marked optional (dev installs use their own venv; sealed bundles
adopt a shipped one), so the old default closure — every non-optional
lockfile package — skipped it and left `hermes` unbootable (audit C05).

Behavioral: drive cmd_install with the real lockfile/registry and stub
installers, asserting on the requested package names, not on script text.
"""

from __future__ import annotations

import argparse
import importlib

import pytest

import pm.cli


@pytest.fixture()
def install_spy(monkeypatch):
    calls = {"names": None, "sync_extras": None, "activated": []}

    def fake_install_names(names, target=None):
        calls["names"] = list(names)
        return 0

    def fake_sync_venv(extras=None, **kwargs):
        calls["sync_extras"] = list(extras or [])
        return None

    def fake_activate(**kwargs):
        calls["activated"].append(kwargs)
        return []

    monkeypatch.setattr(pm.cli, "_install_names", fake_install_names)
    monkeypatch.setattr(importlib.import_module("pm.install"), "sync_venv", fake_sync_venv)
    monkeypatch.setattr(importlib.import_module("pm.install"), "activate", fake_activate)
    return calls


def test_default_closure_includes_the_boot_interpreter(install_spy):
    assert pm.cli.cmd_install(argparse.Namespace(names=None, tools_only=False)) == 0
    assert "python" in install_spy["names"]
    assert "venv" not in install_spy["names"]
    assert all(not pm.cli.get_package(name).internal for name in install_spy["names"])
    assert install_spy["sync_extras"] == ["all"]
    assert install_spy["activated"] == [{"allow_incomplete": True}]


@pytest.mark.parametrize("names", [["npm", "ripgrep"], ["dmgbuild"]])
def test_explicit_names_pass_through_untouched(install_spy, names) -> None:
    assert pm.cli.cmd_install(argparse.Namespace(names=names, tools_only=False)) == 0
    assert install_spy["names"] == names
    assert install_spy["sync_extras"] is None
    assert install_spy["activated"] == []


def test_tools_only_publishes_tools_and_stops_before_the_venv(install_spy):
    assert pm.cli.cmd_install(argparse.Namespace(names=None, extra=[], target=None, tools_only=True)) == 0
    assert "python" in install_spy["names"]
    assert "venv" not in install_spy["names"]
    assert install_spy["sync_extras"] is None
    assert install_spy["activated"] == [{"allow_incomplete": True}]


def test_a_missing_tool_blocks_the_venv_sync(install_spy, monkeypatch, capsys):
    monkeypatch.setattr(
        importlib.import_module("pm.install"), "activate",
        lambda **kwargs: ["git: not installed or outdated"],
    )
    assert pm.cli.cmd_install(argparse.Namespace(names=None, tools_only=False)) == 1
    assert install_spy["sync_extras"] is None
    assert "git: not installed or outdated" in capsys.readouterr().out
