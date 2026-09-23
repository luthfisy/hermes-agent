"""Tag admission for .github/workflows/desktop-bundled-release.yml (plan item 7).

The release build runs under the ``release-signing`` environment, so the
validate job must be more than a shape check: a correctly-shaped tag on an
unreviewed commit must never reach the signing build. Two layers are tested:

* Structure — the workflow declares the admitted SHA as a job output and
  every privileged job checks out THAT, not the (moveable) tag ref.
* Behavior — the production admission command runs inside real temp git
  repositories: an annotated claim on origin/main passes and exports the full
  SHA; a claim for a commit NOT on main is refused.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_WORKFLOW = _REPO / ".github" / "workflows" / "desktop-bundled-release.yml"
_SIGNING_ENV = "release-signing"


def _workflow() -> dict:
    yaml = pytest.importorskip("hermes_yaml")
    return yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))



# ---------------------------------------------------------------------------
# Structure: the admitted SHA is the only build input privileged jobs see.
# ---------------------------------------------------------------------------


def test_validate_exports_the_admitted_sha_as_a_job_output():
    wf = _workflow()
    outputs = wf["jobs"]["validate"].get("outputs") or {}
    assert "sha" in outputs, "validate must export the admitted SHA"
    assert "steps.admission.outputs.sha" in outputs["sha"]


def test_signing_jobs_pin_source_and_controller_revisions_not_mutable_tags():
    wf = _workflow()
    privileged = {
        name: job
        for name, job in wf["jobs"].items()
        if (isinstance(job, dict) and job.get("environment") == _SIGNING_ENV)
    }
    assert privileged, "walk broken: no release-signing jobs found"

    for name, job in privileged.items():
        for step in job.get("steps", []):
            if "checkout" not in step.get("uses", ""):
                continue
            ref = step.get("with", {}).get("ref")
            expected = "${{ needs.validate.outputs.sha }}"
            if (name in {"publish-channel"}
                    or step.get("if") == "needs.validate.outputs.channel-build != ''"
                    or step.get("name") == "Return to the trusted receipt controller"):
                expected = "${{ github.sha }}"
            elif name == "validate":
                expected = "${{ (inputs.build_commit != '' || inputs.channel != '' || inputs.release-phase != '') && github.sha || inputs.tag }}"
            elif name == "assemble-win32-bundle":
                expected = "${{ needs.validate.outputs.channel-build != '' && github.sha || needs.validate.outputs.sha }}"
            assert ref == expected, (
                f"signing job {name!r} checks out {ref!r} — it must check out "
                "the admitted source or explicitly selected trusted controller, never a mutable tag"
            )
    # The merged validate job owns allocation and admission; it consumes no
    # prior admission (the one-dispatch design).
    for name, job in privileged.items():
        if name == "validate":
            assert job.get("needs") in ([], None)
            continue
        needs = job.get("needs") or []
        needs = [needs] if isinstance(needs, str) else needs
        assert "validate" in needs, f"signing job {name!r} reads needs.validate.outputs.sha but does not need validate"


# ---------------------------------------------------------------------------
# Behavior: run the admission script against real repositories.
# ---------------------------------------------------------------------------

bash = shutil.which("bash")
pytestmark = pytest.mark.skipif(bash is None, reason="bash is required to run the admission script")


def _native_tool(name: str) -> str:
    """Resolve *name* to an executable CreateProcess can actually start.

    run_tests.sh / conftest blank SystemRoot/ComSpec for hermeticity, and on
    this host PATH can point ``git``/``bash`` at the MSIX payload copy under
    ``C:\\Program Files\\WindowsApps\\...`` — a store-app location that
    fails CreateProcess with WinError 5 outside its package context. Prefer
    a conventional install; give children a complete environment too.
    """
    candidates = [hit for hit in (shutil.which(name),) if hit]
    if sys.platform == "win32":
        git_base = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git"
        for rel in (("cmd", f"{name}.exe"), ("bin", f"{name}.exe"), ("usr", "bin", f"{name}.exe")):
            p = git_base.joinpath(*rel)
            if p.exists():
                candidates.append(str(p))
    for cand in candidates:
        if "windowsapps" not in cand.lower():
            return cand
    return candidates[0]


def _child_env(**overrides: str) -> dict:
    env = os.environ.copy()
    if sys.platform == "win32":
        env.setdefault("SystemRoot", r"C:\Windows")
        env.setdefault("ComSpec", r"C:\Windows\system32\cmd.exe")
        env.setdefault("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    env.update(overrides)
    return env


_GIT = _native_tool("git")
_BASH = _native_tool("bash")


def _git(*args: str, cwd: Path) -> str:
    out = subprocess.run(
        [_GIT, *args], cwd=cwd, capture_output=True, text=True, check=True,
        env=_child_env(),
    )
    return out.stdout.strip()


def _seed_repo(root: Path) -> tuple[Path, Path]:
    """origin (upstream) + clone (where releases are tagged from).

    origin/main holds a pyproject whose version matches the stable tag, so
    the pyproject-lockstep leg of the shape check passes for v0.1.2.
    """
    origin = root / "origin"
    origin.mkdir()
    _git("init", "-b", "main", cwd=origin)
    _git("config", "user.email", "ci@example.com", cwd=origin)
    _git("config", "user.name", "ci", cwd=origin)
    (origin / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "0.1.2"\n', encoding="utf-8")
    (origin / "README.md").write_text("seed\n", encoding="utf-8")
    _git("add", "-A", cwd=origin)
    _git("commit", "-m", "seed", cwd=origin)

    clone = root / "clone"
    _git("clone", str(origin), str(clone), cwd=root)
    _git("config", "user.email", "ci@example.com", cwd=clone)
    _git("config", "user.name", "ci", cwd=clone)
    return origin, clone


def _run_admission(clone: Path, tag: str, claim_tag: str) -> subprocess.CompletedProcess:
    gh_output = clone / "github_output.txt"
    gh_output.write_text("", encoding="utf-8")
    env = _child_env(
        TAG=tag,
        RELEASE_TAG=tag,
        RELEASE_CLAIM_TAG=claim_tag,
        RELEASE_CLAIM_OBJECT=_git("rev-parse", f"refs/tags/{claim_tag}", cwd=clone),
        GITHUB_OUTPUT=str(gh_output),
        RELEASE_PHASE="candidate",
        GITHUB_REF=f"refs/tags/{claim_tag}",
        GITHUB_SHA=_git("rev-parse", "HEAD", cwd=clone),
        PYTHONPATH=str(_REPO),
    )
    return subprocess.run(
        [sys.executable, "-m", "scripts.releases.stable", "verify"],
        cwd=clone,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _claim_message(clone: Path) -> str:
    return json.dumps({
        "schema": 1,
        "version": "0.1.2",
        "commit": _git("rev-parse", "HEAD", cwd=clone),
        "autopublish": False,
        "claimEpoch": 1_790_000_000,
    }, sort_keys=True, separators=(",", ":"))


def test_claim_on_origin_main_is_admitted_and_exports_the_full_sha(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _origin, clone = _seed_repo(tmp_path)
    monkeypatch.setenv("GIT_COMMITTER_DATE", "@1790000000 +0000")
    _git("tag", "-a", "v0.1.2-rc", "-m", _claim_message(clone), cwd=clone)
    _git("push", "origin", "refs/tags/v0.1.2-rc", cwd=clone)

    proc = _run_admission(clone, "v0.1.2", "v0.1.2-rc")
    assert proc.returncode == 0, proc.stdout + proc.stderr

    gh_output = (clone / "github_output.txt").read_text(encoding="utf-8")
    expected = _git("rev-parse", "HEAD", cwd=clone)
    assert f"sha={expected}" in gh_output


def test_claim_not_on_origin_main_is_refused(tmp_path: Path):
    _origin, clone = _seed_repo(tmp_path)
    # A commit that exists ONLY in the clone — never pushed, never reviewed.
    (clone / "rogue.txt").write_text("unreviewed\n", encoding="utf-8")
    _git("add", "-A", cwd=clone)
    _git("commit", "-m", "rogue", cwd=clone)
    _git("tag", "-a", "v0.1.2-rc", "-m", _claim_message(clone), cwd=clone)
    _git("push", "origin", "refs/tags/v0.1.2-rc", cwd=clone)

    proc = _run_admission(clone, "v0.1.2", "v0.1.2-rc")
    assert proc.returncode != 0, "a tag off origin/main must not be admitted"
    assert "is not on main" in proc.stdout + proc.stderr
    # And nothing was exported for the signing jobs to consume.
    assert "sha=" not in (clone / "github_output.txt").read_text(encoding="utf-8")


def test_malformed_claim_is_refused(tmp_path: Path):
    _origin, clone = _seed_repo(tmp_path)
    _git("tag", "-a", "v0.1.2-rc1", "-m", "bad claim", cwd=clone)
    _git("push", "origin", "refs/tags/v0.1.2-rc1", cwd=clone)
    proc = _run_admission(clone, "v0.1.2", "v0.1.2-rc1")
    assert proc.returncode != 0
    assert "is not a claim tag" in proc.stdout + proc.stderr


def _require_step(job: str, name_prefix: str) -> dict:
    steps = _workflow()["jobs"][job]["steps"]
    (step,) = [s for s in steps if isinstance(s, dict) and s.get("name", "").startswith(name_prefix)]
    return step


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_downloadable_windows_builds_refuse_to_ship_unsigned(tmp_path: Path):
    """The Windows signer only warns without AZURE_SIGN_*; every lane whose
    artifacts are downloadable must therefore fail before building, under
    the same gate the macOS leg uses for its signing credentials."""
    step = _require_step("build-win32-release", "Require Azure signing")
    assert step["if"] == _require_step("build-darwin-release", "Require signing credentials")["if"]
    assert "build-commit" not in step["if"] and "release-phase == 'candidate'" in step["if"]
    names = list(step["env"])
    assert {"AZURE_SIGN_ENDPOINT", "AZURE_SIGN_ACCOUNT", "AZURE_SIGN_PROFILE", "AZURE_CLIENT_ID"} <= set(names)

    def run(**values: str) -> subprocess.CompletedProcess:
        env = {"PATH": os.environ["PATH"], "RUNNER_TEMP": str(tmp_path)}
        env.update({name: "" for name in names})
        env.update(values)
        return subprocess.run(["bash", "-euo", "pipefail", "-c", step["run"]], env=env,
                              capture_output=True, text=True, timeout=60)

    proc = run()
    assert proc.returncode != 0 and "::error::" in proc.stdout and "AZURE_SIGN_ENDPOINT" in proc.stdout
    assert run(**{name: "x" for name in names}).returncode == 0
    partial = run(**{name: "x" for name in names if name != "AZURE_CLIENT_ID"})
    assert partial.returncode != 0 and "AZURE_CLIENT_ID" in partial.stdout
