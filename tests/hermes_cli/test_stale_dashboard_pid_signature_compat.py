"""Cross-version update: a stale ``main_dashboard`` finder must still be callable.

``hermes update`` runs its post-swap tail in the PRE-pull interpreter (or one that
cached the old module), while ``dashboard_procs`` — the module that actually calls
the finder — is fresh. A kwarg added to ``_find_stale_dashboard_pids`` between the
two generations (``scope_home``, #113978) then made the new caller invoke an OLD
callee with a shape it does not accept:

    TypeError: _find_stale_dashboard_pids() got an unexpected keyword argument 'scope_home'

which killed post-update cleanup after the code swap had already succeeded (#117305).

These tests pin the shape tolerance without running an update: the old-shaped
finder is handed to the caller directly.
"""

from __future__ import annotations

from unittest import mock

from hermes_cli import dashboard_procs, main_dashboard

OWN_HOME = "/home/u/.hermes"
FOREIGN_HOME = "/home/other/.hermes"


def _old_shaped_finder(*, exclude_pids=None):
    """The pre-#113978 finder: no ``scope_home`` parameter at all."""
    return [111, 222, 333]


def _old_shaped_dash():
    """A ``main_dashboard`` from before the ``scope_home`` kwarg existed."""
    dash = mock.Mock()
    dash._find_stale_dashboard_pids = _old_shaped_finder
    return dash


def test_post_update_cleanup_survives_an_old_shaped_finder(monkeypatch):
    """The reported crash, end to end through the update cleanup entry point.

    Pre-fix the fresh caller passes ``scope_home`` into the old callee and the
    update dies with a TypeError *after* the code swap already succeeded.
    """
    calls: list[dict] = []

    def _find(*, exclude_pids=None):
        calls.append({"exclude_pids": exclude_pids})
        return []

    monkeypatch.setattr(main_dashboard, "_find_stale_dashboard_pids", _find)
    monkeypatch.setattr(main_dashboard, "_restart_managed_dashboard_service", lambda *a, **k: False)
    monkeypatch.setattr(dashboard_procs, "_lock_owned_serve_pids", lambda: set())

    result = dashboard_procs._kill_stale_dashboard_processes(
        restart_managed=True, scope_home=OWN_HOME
    )

    assert calls == [{"exclude_pids": None}], "scope_home reached an old-shaped finder"
    assert result == {"matched": [], "killed": [], "failed": []}


def test_post_update_cleanup_still_scopes_the_sweep_to_the_updating_home(monkeypatch):
    """Dropping the kwarg is NOT an acceptable fix: another home's backend must be spared.

    A pre-#113978 callee cannot scope the sweep itself, so the caller applies the
    same home filter — otherwise the update would stop a foreign install's backend.
    """
    killed: list[int] = []

    def _kill(pids, _killed, _failed):
        killed.extend(pids)

    def _find(*, exclude_pids=None):
        return [111, 222, 333]

    monkeypatch.setattr(main_dashboard, "_find_stale_dashboard_pids", _find)
    monkeypatch.setattr(main_dashboard, "_restart_managed_dashboard_service", lambda *a, **k: False)
    monkeypatch.setattr(dashboard_procs, "_lock_owned_serve_pids", lambda: set())
    monkeypatch.setattr(
        dashboard_procs, "_hermes_home_for_pid",
        lambda pid: {111: OWN_HOME, 222: FOREIGN_HOME, 333: None}[pid],
    )
    monkeypatch.setattr(dashboard_procs, "_kill_pids_windows", _kill)
    monkeypatch.setattr(dashboard_procs, "_kill_pids_posix", _kill)

    dashboard_procs._kill_stale_dashboard_processes(restart_managed=True, scope_home=OWN_HOME)

    # Foreign home (222) and unreadable ownership (333) are spared; only our own backend stops.
    assert killed == [111]


def test_old_shaped_finder_is_called_without_scope_home():
    """The seam itself: the callee is probed before the kwarg is passed."""
    pids = dashboard_procs._stale_pids(_old_shaped_dash(), exclude_pids=None, scope_home=None)
    assert pids == [111, 222, 333]


def test_old_shaped_finder_still_filters_to_the_updating_home(monkeypatch):
    """Dropping the kwarg must not widen the sweep: the home filter runs here instead.

    Simply ignoring ``scope_home`` would be a worse bug than the crash — the update
    sweep would then stop another install's (or profile's) backend, which #113978
    exists to prevent. Unreadable ownership is spared, never guessed.
    """
    monkeypatch.setattr(
        dashboard_procs, "_hermes_home_for_pid",
        lambda pid: {111: OWN_HOME, 222: FOREIGN_HOME, 333: None}[pid],
    )

    pids = dashboard_procs._stale_pids(_old_shaped_dash(), exclude_pids=None, scope_home=OWN_HOME)
    assert pids == [111]


def test_new_shaped_finder_receives_scope_home():
    """No behaviour change on the happy path: a current callee is called as before."""
    dash = mock.Mock()
    dash._find_stale_dashboard_pids = lambda **kwargs: [kwargs["scope_home"]]

    pids = dashboard_procs._stale_pids(dash, exclude_pids=None, scope_home=OWN_HOME)
    assert pids == [OWN_HOME]


def test_kwargs_finder_is_called_with_scope_home():
    """A ``**kwargs`` callee (test doubles, future shapes) is never probed down."""
    dash = mock.Mock()
    dash._find_stale_dashboard_pids = lambda **kwargs: [kwargs["scope_home"]]

    pids = dashboard_procs._stale_pids(dash, exclude_pids=None, scope_home=OWN_HOME)
    assert pids == [OWN_HOME]
