"""The release workflow's dependency graph enforces publication ordering."""
import os
import subprocess
from pathlib import Path

from ruamel.yaml import YAML

ROOT = Path(__file__).resolve().parents[2]


def workflow(name):
    return YAML(typ="base").load((ROOT / ".github/workflows" / name).read_text(encoding="utf-8"))


def ancestors(jobs, name):
    seen = set()
    pending = [name]
    while pending:
        needs = jobs[pending.pop()].get("needs", [])
        for item in [needs] if isinstance(needs, str) else needs:
            if item not in seen:
                seen.add(item)
                pending.append(item)
    return seen


def test_admit_can_read_the_claim_draft():
    jobs = workflow("stable-release.yml")["jobs"]
    assert jobs["admit"]["permissions"]["contents"] == "write"


def test_release_reuses_whole_ci_and_docker_before_publication():
    jobs = workflow("stable-release.yml")["jobs"]
    assert jobs["ci"]["uses"] == "./.github/workflows/ci.yaml"
    assert jobs["ci"]["with"]["release"] == "true"
    assert "secrets" not in jobs["ci"]
    assert jobs["docker"]["uses"] == jobs["publish-docker"]["uses"]
    assert jobs["docker"]["with"]["release-phase"] == "test"
    assert "ci" in ancestors(jobs, "docker")
    required = {"ci", "docker", "nix", "pm-bundle", "install-e2e", "windows-packaged", "macos-packaged", "termux-checks", "windows-live", "candidates", "bootstrap-version"}
    assert required <= ancestors(jobs, "acceptance")
    for name in ("publish-docker", "publish-bundles"):
        assert required <= ancestors(jobs, name)
    assert {"publish-docker", "publish-bundles", "publication"} <= ancestors(jobs, "complete")
    assert "promote-docker" not in jobs and "promote-bundles" not in jobs
    for name in ("acceptance", "publication", "complete"):
        assert jobs[name]["if"] == "always()"


def test_all_applicable_ci_jobs_are_aggregated_and_desktop_e2e_stays_deferred():
    jobs = workflow("ci.yaml")["jobs"]
    checks = {name for name, job in jobs.items() if "uses" in job}
    assert checks <= set(jobs["all-checks-pass"]["needs"])
    assert jobs["e2e-desktop"]["if"] == "false"
    assert "workflow_call" in workflow("ci.yaml")["on"]


def test_claim_custody_and_final_payload_identity_reach_every_privileged_phase():
    release = workflow("stable-release.yml")
    jobs = release["jobs"]
    assert "autopublish" not in release["on"]["workflow_dispatch"]["inputs"]
    assert {"claim-tag", "claim-object", "tag", "commit", "version", "release-id", "release-epoch"} <= \
        set(jobs["admit"]["outputs"])
    for name in ("candidates", "publish-bundles"):
        call = jobs[name]["with"]
        assert call["tag"] == "${{ needs.admit.outputs.tag }}"
        assert call["claim-tag"] == "${{ needs.admit.outputs.claim-tag }}"
        assert call["claim-object"] == "${{ needs.admit.outputs.claim-object }}"
    desktop = workflow("desktop-bundled-release.yml")
    assert "release-epoch" in desktop["jobs"]["validate"]["outputs"]
    assert desktop["jobs"]["termux-deb"]["env"]["HERMES_RELEASE_EPOCH"] == \
        "${{ needs.validate.outputs.release-epoch }}"
    for name in ("docker", "nix", "pm-bundle"):
        assert jobs[name]["with"]["version"] == "${{ needs.admit.outputs.version }}"
    for name in ("docker", "nix", "pm-bundle"):
        assert jobs[name]["with"]["release-epoch"] == "${{ needs.admit.outputs.release-epoch }}"
    assert jobs["publish-docker"]["with"]["release-epoch"] == \
        "${{ needs.admit.outputs.release-epoch }}"
    complete = jobs["complete"]["steps"]
    final = next(i for i, step in enumerate(complete) if step.get("name", "").startswith("Create the final tag"))
    assert complete[final]["env"]["DOCKER_MANIFEST_DIGEST"] == \
        "${{ needs.publish-docker.outputs.manifest-digest }}"
    render = next(i for i, step in enumerate(complete) if step.get("name", "").startswith("Render the admitted"))
    reconcile = next(i for i, step in enumerate(complete) if step.get("name", "").startswith("Reconcile ordered"))
    assert final < render < reconcile


def test_docker_dev_stamp_checkout_has_release_history():
    build = workflow("docker.yml")["jobs"]["build"]
    checkout = next(step for step in build["steps"] if "actions/checkout@" in step.get("uses", ""))

    assert checkout["with"]["fetch-depth"] == "0"


def test_release_gates_extract_consumer_facing_versions():
    release_jobs = workflow("stable-release.yml")["jobs"]
    bootstrap = next(
        step["run"] for step in release_jobs["bootstrap-version"]["steps"]
        if step.get("name") == "Stamp and verify the Cargo and Tauri release identity"
    )
    assert "uv build --wheel --sdist" in bootstrap
    assert "wheel_version != expected or sdist_version != expected" in bootstrap

    docker = workflow("docker.yml")["jobs"]
    image_check = next(
        step["run"] for step in docker["build"]["steps"]
        if step.get("name") == "Verify release image identity"
    )
    assert '["baseVersion"]' in image_check and "$RELEASE_VERSION" in image_check

    nix = workflow("nix.yml")["jobs"]
    nix_check = next(
        step["run"] for step in nix["flake-check"]["steps"]
        if step.get("name") == "Verify release package runtime identity"
    )
    assert '"$package/bin/hermes" --version' in nix_check
    assert "actual != expected" in nix_check


def test_publication_reconciler_has_every_recovery_trigger_and_shared_lock():
    stable = workflow("stable-release.yml")
    publication = workflow("stable-release-publication.yml")

    assert stable["concurrency"] == publication["concurrency"] == {
        "group": "stable-release", "cancel-in-progress": "false",
    }
    assert {"workflow_dispatch", "workflow_run", "schedule"} <= set(publication["on"])
    assert publication["on"]["workflow_run"] == {
        "workflows": ["Stable Release"], "types": ["completed"],
    }
    reconcile = publication["jobs"]["reconcile"]
    assert reconcile["environment"] == "release-signing"
    assert publication["permissions"] == {"contents": "write", "actions": "write"}
    assert "conclusion != 'success'" in reconcile["if"]
    checkout = reconcile["steps"][0]
    assert checkout["with"]["ref"] == "${{ github.event.repository.default_branch }}"
    assert checkout["with"]["persist-credentials"] == "false"
    assert publication["on"]["schedule"] == [{"cron": "*/15 * * * *"}]
    assert not any(step.get("run", "").startswith("sleep ") for step in reconcile["steps"])


def test_docker_recovery_refuses_to_replace_a_divergent_version_tag(tmp_path):
    publish = workflow("docker.yml")["jobs"]["release-publish-manifest"]
    step = next(item for item in publish["steps"] if item.get("name") == "Create versioned manifest list")
    digest_dir = tmp_path / "digests"
    digest_dir.mkdir()
    for arch, digest in (("amd64", "a" * 64), ("arm64", "b" * 64)):
        (digest_dir / f"{arch}.digest").write_text(f"sha256:{digest}\n", encoding="utf-8")

    marker = tmp_path / "create-called"
    bindir = tmp_path / "bin"
    bindir.mkdir()
    docker = bindir / "docker"
    docker.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, pathlib, sys\n"
        "if 'create' in sys.argv:\n"
        "    pathlib.Path(os.environ['CREATE_MARKER']).write_text('called')\n"
        "    raise SystemExit(0)\n"
        "print(json.dumps({'manifests': [{'digest': 'sha256:' + 'c' * 64}]}))\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c",
         step["run"].replace("/tmp/digests", str(digest_dir))], cwd=tmp_path,
        env={**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}",
             "IMAGE_NAME": "owner/repo", "RELEASE_TAG": "0.21.5",
             "CREATE_MARKER": str(marker)},
        capture_output=True, text=True, encoding="utf-8",
    )
    assert result.returncode != 0
    assert "versioned Docker manifest differs" in result.stderr
    assert not marker.exists()
