"""Marker settlement uses its own inventory, never a historical receipt's ownership."""

import json

import pytest

from hermes_cli import process_identity, update_cmd_fleet as fleet, update_receipt
from hermes_constants import get_hermes_home
import hermes_cli.update_host_obligation as host_obligation

MANUAL = {"kind": "serve", "profile": "work", "pid": 900, "supervisor": "manual-serve", "restart_via": "respawn-argv", "code_sha": "old", "detail": {"create_time": 1000.0}}
CURRENT = {"profile": "alpha", "state": "current", "code_sha": "new"}
GATEWAY = {"kind": "gateway", "profile": "alpha", "code_sha": "old"}

CASES = [
    ("receipt-successor", {"outcome": "failed", "plan": {"runtimes": [GATEWAY]}}, None, [CURRENT], False),
    ("marker-external-restart", {}, "new", [CURRENT], False),
    ("missing-sibling", {"outcome": "failed", "plan": {"runtimes": [GATEWAY, dict(GATEWAY, profile="beta")]}}, "new", [CURRENT], True),
    ("stale-successor", {"outcome": "failed", "plan": {"runtimes": [GATEWAY]}}, None, [dict(CURRENT, state="stale", code_sha="old")], True),
    ("unknown-successor", {"outcome": "failed", "plan": {"runtimes": [GATEWAY]}}, None, [dict(CURRENT, state="unknown")], True),
    ("marker-no-sha", {}, "", [CURRENT], True),
    ("checkout-moved", {}, "old", [CURRENT], True),
    ("marker-empty-no-receipt", {}, "new", [], True),
    ("markerless-stamped-manual", {"outcome": "partial", "plan": {"runtimes": [MANUAL]}}, None, [], False),
    ("old-manual-new-marker", {"outcome": "success", "post_update": {"sha": "old"}, "plan": {"runtimes": [MANUAL]}, "fleet": []}, "new", [], True),
    ("same-sha-not-ownership", {"outcome": "success", "post_update": {"sha": "new"}, "plan": {"runtimes": [MANUAL]}, "fleet": []}, "new", [], True),
    ("mixed-receipt-successors", {"outcome": "partial", "plan": {"runtimes": [GATEWAY, MANUAL]}, "fleet": [dict(CURRENT, state="stale", code_sha="old")]}, None, [CURRENT], False),
]


def seed(monkeypatch, old, marker, live, alive=True):
    root = get_hermes_home() / "logs" / "update_receipts"
    root.mkdir(parents=True, exist_ok=True)
    target = root / "latest.json"
    target.write_text(json.dumps(old))
    monkeypatch.setattr(process_identity, "_pid_alive_matches", lambda *a: alive)
    monkeypatch.setattr(fleet, "_current_checkout_sha", lambda: "new")
    monkeypatch.setattr("hermes_cli.update_cmd._current_checkout_sha", lambda: "new")
    monkeypatch.setattr(update_receipt, "collect_fleet_versions", lambda **k: live)
    if marker is not None:
        fleet._write_fleet_restart_pending_marker(expected_sha=marker)
    return target


@pytest.mark.parametrize("name,old,marker,live,pending", CASES, ids=[case[0] for case in CASES])
def test_scoped_reconciliation_matrix(monkeypatch, capsys, name, old, marker, live, pending):
    live = list(live)
    target = seed(monkeypatch, old, marker, live)
    if name in ("marker-external-restart", "missing-sibling", "checkout-moved"):
        runtimes = [GATEWAY, dict(GATEWAY, profile="beta")] if name == "missing-sibling" else [GATEWAY]
        fleet._write_fleet_restart_pending_marker(expected_sha=marker, runtimes=runtimes)
    before = target.read_bytes()
    assert fleet._pending_fleet_restart_needed() is pending
    fleet._warn_pending_fleet_restart_on_startup()
    assert ("hermes gateway restart" in capsys.readouterr().err) is pending
    # Deferred catch-up rides the ordinary completion owner under PM; the marker
    # lifecycle is what the startup warning reflects here.
    assert target.read_bytes() == before
    assert host_obligation.host_obligation_path().exists() is (marker is not None and pending)
    if name == "missing-sibling":
        live.append(dict(CURRENT, profile="beta"))
        assert not fleet._pending_fleet_restart_needed()
        assert not host_obligation.host_obligation_path().exists()
        assert target.read_bytes() == before


@pytest.mark.parametrize("suffix", ["{", '{"version": 1, "inventory": ', "broken-line"])
def test_malformed_obligation_stays_pending(monkeypatch, suffix):
    """An unparseable record is an obligation whose terms are unknown — never a discharged one."""
    seed(monkeypatch, {}, "new", [CURRENT])
    marker = host_obligation.host_obligation_path()
    marker.write_text(marker.read_text(encoding="utf-8") + suffix, encoding="utf-8")
    assert fleet._pending_fleet_restart_needed()
    assert marker.exists()


def test_marker_reconciliation_collects_one_live_snapshot(monkeypatch):
    seed(monkeypatch, {}, "new", [CURRENT])
    fleet._write_fleet_restart_pending_marker(expected_sha="new", runtimes=[GATEWAY])
    probes = []

    def collect(**kwargs):
        probes.append(True)
        return [CURRENT] if len(probes) == 1 else []

    monkeypatch.setattr(update_receipt, "collect_fleet_versions", collect)
    assert not fleet._pending_fleet_restart_needed()
    assert len(probes) == 1


@pytest.mark.parametrize("completed_restart", [False, True])
def test_legacy_marker_discharges_on_live_fleet_evidence_without_receipt(monkeypatch, capsys, completed_restart):
    """An inventory-less N+1 marker settles on live-fleet evidence alone (#115638).

    It never borrows the old receipt's ownership: the receipt is left intact and the
    marker discharges only because every live row is current at its expected SHA.
    """
    old = {"outcome": "failed", "plan": {"runtimes": [GATEWAY]}}
    if completed_restart:
        old.update(post_update={"sha": "new"}, gateway_restart={"incomplete": False})
    live = [CURRENT]
    target = seed(monkeypatch, old, "new", live)
    marker = host_obligation.host_obligation_path()
    receipt_before = target.read_bytes()
    fleet._warn_pending_fleet_restart_on_startup()
    assert "hermes gateway restart" not in capsys.readouterr().err
    assert not fleet._pending_fleet_restart_needed()
    assert not marker.exists()
    assert target.read_bytes() == receipt_before


@pytest.mark.parametrize("live,pending", [([CURRENT], False), ([dict(CURRENT, state="stale", code_sha="old")], True), ([], True)], ids=["fleet-current", "fleet-stale", "fleet-empty"])
def test_inventory_less_marker_settles_after_out_of_band_pull(monkeypatch, capsys, live, pending):
    """An inventory-less marker left behind by an old update survives every later out-of-band
    ``git pull`` (#115638): nothing rewrites it, and its ``expected_sha`` is never HEAD again.
    It records no owed set, so a fleet that is current on the checkout is the whole of the
    evidence the warning can be about — a stale or absent fleet still keeps it.
    """
    seed(monkeypatch, {}, "old", live)
    marker = host_obligation.host_obligation_path()
    fleet._warn_pending_fleet_restart_on_startup()
    assert ("hermes gateway restart" in capsys.readouterr().err) is pending
    assert fleet._pending_fleet_restart_needed() is pending
    assert marker.exists() is pending
