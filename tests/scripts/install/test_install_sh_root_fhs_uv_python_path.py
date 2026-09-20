"""Regression test for install.sh root-mode uv Python install path.

When installing as root with the FHS layout (INSTALL_DIR=/usr/local/lib/...),
``uv python install`` must place the managed Python under a world-readable
location, otherwise the venv interpreter ends up at ``/root/.local/share/uv/...``
and the shared ``/usr/local/bin/hermes`` wrapper fails for non-root users with
"bad interpreter: Permission denied".  See #21457.
"""

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"


def _resolve_install_layout_body() -> str:
    """Return just the body of resolve_install_layout(), bounded by its
    opening signature and the next top-level ``}`` close brace.

    Using the function body (not "first ``return 0`` after a marker") guards
    the tests below against future refactors that hoist the export above
    another conditional with its own early-return, or that insert an early-
    return between the marker and the export — both of which would leave the
    export unreachable while a less-strict assertion still passed.
    """
    text = INSTALL_SH.read_text(encoding="utf-8")
    head, _, rest = text.partition("resolve_install_layout() {\n")
    assert rest, "Could not find resolve_install_layout() in scripts/install.sh"
    body, _, _ = rest.partition("\n}\n")
    assert body, "Could not find resolve_install_layout() closing brace"
    return body


def test_root_fhs_layout_routes_through_contract_function() -> None:
    """Wiring guard: the root branch must route its uv state through the shared
    ``uv_isolated_state_env`` rather than pinning it inline — an inline copy is
    what the isolation invariant could silently drift away from (#21457).

    The *values* and their no-inheritance are proven by execution (hostile
    inherited ``UV_*`` in, final environment asserted) in
    ``tests/scripts/install/test_install_sh_uv_isolation.py::test_fhs_root_layout_defeats_hostile_inherited_uv_state``.
    The cross-language lockstep with Python lives in
    ``test_uv_isolation_contract.py``.
    """
    body = _resolve_install_layout_body()

    assert "uv_isolated_state_env fhs-root" in body, (
        "the root branch must route its uv state through uv_isolated_state_env "
        "(one source of truth for both install kinds)"
    )
    assert "UV_PYTHON_INSTALL_DIR=" not in body, (
        "the root branch must not pin uv state itself — that copy is what the "
        "isolation invariant could silently drift away from"
    )


def test_root_fhs_uv_state_env_is_called_inside_root_branch() -> None:
    """The call must live in the root-FHS branch of resolve_install_layout,
    after ``ROOT_FHS_LAYOUT=true`` and before the branch's ``return 0``, so
    non-root and Termux installs are unaffected. Bound the slice by the
    function body (not "next return 0" in the whole file) so the assertion
    can't accept an unreachable call."""
    body = _resolve_install_layout_body()

    marker = 'ROOT_FHS_LAYOUT=true'
    assert marker in body
    after_marker = body.split(marker, 1)[1]
    return_idx = after_marker.find('return 0')
    call_idx = after_marker.find('uv_isolated_state_env fhs-root')
    assert call_idx != -1, "uv_isolated_state_env call missing from root-FHS branch"
    assert return_idx != -1, "root-FHS branch must end with `return 0`"
    assert call_idx < return_idx, (
        "Call must precede the branch's `return 0` — otherwise unreachable"
    )
