"""The release entrypoint claims a version, cuts a draft, and dispatches the gate.

The claim is the tag push, and it pushes exactly the claim ref. A release that
never starts is an error, not a warning the operator has to notice.
"""
import json
import subprocess
import threading

import pytest


def git(repo, *args):
    return subprocess.check_output(["git", *args], cwd=repo, text=True, encoding="utf-8").strip()


@pytest.fixture
def source(tmp_path):
    origin = tmp_path / "origin.git"
    repo = tmp_path / "source"
    origin.mkdir()
    repo.mkdir()
    git(origin, "init", "--bare", "--quiet")
    git(repo, "init", "--initial-branch=main", "--quiet")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.test")
    (repo / "pyproject.toml").write_text('[project]\nname="fixture"\nversion="0.0.0"\n', encoding="utf-8")
    git(repo, "add", "pyproject.toml")
    git(repo, "commit", "--quiet", "-m", "Initial")
    git(repo, "remote", "add", "origin", str(origin))
    git(repo, "push", "--quiet", "-u", "origin", "main")
    return repo


def _claim(repo, version, commit):
    metadata = json.dumps({
        "schema": 1, "version": version, "commit": commit,
        "autopublish": False, "claimEpoch": 1_790_000_000,
    }, sort_keys=True, separators=(",", ":"))
    git(repo, "tag", "-a", f"v{version}-rc", commit, "-m",
        metadata)
    git(repo, "push", "--quiet", "origin", f"refs/tags/v{version}-rc")


def test_release_claims_the_derived_version_creates_a_draft_and_dispatches(source):
    from scripts.releases.entrypoint import release

    commit = git(source, "rev-parse", "HEAD")
    calls = []

    def execute(command):
        calls.append(command)
        if command[:3] == ["gh", "run", "list"]:
            return json.dumps([{"databaseId": 7, "url": "https://github.com/example/hermes-agent/actions/runs/7",
                                "headBranch": "v0.21.5-rc", "status": "queued"}])
        return ""

    result = release(commit, bump="patch", repo=source, remote="origin",
                     repository="example/hermes-agent", execute=execute, autopublish=True)

    assert result["version"] == "0.21.5"
    assert result["tag"] == "v0.21.5-rc"
    assert result["commit"] == commit
    assert result["url"] == "https://github.com/example/hermes-agent/releases/tag/v0.21.5-rc"
    assert result["final_url"] == "https://github.com/example/hermes-agent/releases/tag/v0.21.5"
    assert git(source, "rev-parse", "v0.21.5-rc^{commit}") == commit
    claim = json.loads(git(source, "tag", "-l", "v0.21.5-rc", "--format=%(contents)"))
    assert isinstance(claim.pop("claimEpoch"), int)
    assert claim == {
        "autopublish": True,
        "commit": commit,
        "schema": 1,
        "version": "0.21.5",
    }
    assert calls == [
        ["gh", "release", "create", "v0.21.5-rc", "--repo", "example/hermes-agent",
         "--verify-tag", "--draft", "--generate-notes", "--title", "Hermes Agent v0.21.5"],
        ["gh", "workflow", "run", "stable-release.yml", "--ref", "v0.21.5-rc",
         "--repo", "example/hermes-agent", "--raw-field", "tag=v0.21.5-rc"],
        ["gh", "run", "list", "--repo", "example/hermes-agent", "--workflow", "stable-release.yml",
         "--branch", "v0.21.5-rc", "--json", "databaseId,url,headBranch,status"],
    ]
    assert result["run_url"] == "https://github.com/example/hermes-agent/actions/runs/7"
    # The claim push names the claim ref and nothing else.
    pushed = git(source, "ls-remote", "origin", "refs/tags/v0.21.5-rc")
    assert pushed.startswith(git(source, "rev-parse", "v0.21.5-rc"))


def test_release_output_names_the_wait_and_the_publish_step():
    from scripts.releases.entrypoint import next_steps

    result = {"version": "0.21.5", "tag": "v0.21.5-rc", "autopublish": False,
              "run_url": "https://github.com/example/hermes-agent/actions/runs/7",
              "final_url": "https://github.com/example/hermes-agent/releases/tag/v0.21.5"}
    text = next_steps(result)
    assert "Workflow: " + result["run_url"] in text
    assert "The release workflow started on v0.21.5-rc." in text
    assert "Wait for that workflow to finish." in text
    assert result["final_url"] in text
    assert "python scripts/release.py publish --version 0.21.5 --remote origin" in text

    automatic = next_steps({**result, "autopublish": True})
    assert "Autopublish is on." in automatic
    assert "publish --version" not in automatic


def test_a_commit_behind_an_outstanding_claim_is_refused(source):
    from scripts.releases.entrypoint import ReleaseRefused, _require_ancestry, release

    earlier = git(source, "rev-parse", "HEAD")
    git(source, "commit", "--allow-empty", "--quiet", "-m", "later")
    git(source, "push", "--quiet", "origin", "main")
    _claim(source, "0.21.5", git(source, "rev-parse", "HEAD"))

    with pytest.raises(ReleaseRefused, match="0\\.21\\.5 already claimed"):
        release(earlier, bump="patch", repo=source, remote="origin",
                repository="example/hermes-agent", execute=lambda _cmd: pytest.fail("must not execute"))
    assert "v0.21.6-rc" not in git(source, "tag", "--list")

    _require_ancestry(source, git(source, "rev-parse", "v0.21.5-rc^{commit}"))


def test_successive_claims_reserve_increasing_native_epochs(source):
    from scripts.releases.entrypoint import release

    first = git(source, "rev-parse", "HEAD")
    release(first, bump="patch", repo=source, remote="origin",
            repository="example/hermes-agent", execute=lambda _command: None)
    git(source, "commit", "--allow-empty", "--quiet", "-m", "next")
    git(source, "push", "--quiet", "origin", "main")
    second = git(source, "rev-parse", "HEAD")
    release(second, bump="patch", repo=source, remote="origin",
            repository="example/hermes-agent", execute=lambda _command: None)

    first_epoch = int(git(source, "for-each-ref", "refs/tags/v0.21.5-rc",
                          "--format=%(taggerdate:unix)"))
    second_epoch = int(git(source, "for-each-ref", "refs/tags/v0.21.6-rc",
                           "--format=%(taggerdate:unix)"))
    assert second_epoch > first_epoch


def test_a_dispatch_that_never_starts_is_an_error(source):
    from scripts.releases.entrypoint import ReleaseRefused, release

    def refuse(command):
        if command[1:3] == ["workflow", "run"]:
            raise RuntimeError("workflow dispatch rejected")

    with pytest.raises(ReleaseRefused, match="never started"):
        release(git(source, "rev-parse", "HEAD"), bump="patch", repo=source, remote="origin",
                repository="example/hermes-agent", execute=refuse)
    # The claim stands unresolved; reconciliation burns it only after its grace period.
    assert "v0.21.5-rc" in git(source, "tag", "--list")


def test_publish_and_abandon_output_name_the_result():
    from scripts.releases.entrypoint import abandon_steps, publish_steps

    published = publish_steps({"version": "0.21.5", "repository": "example/hermes-agent",
                               "run_url": "https://github.com/example/hermes-agent/actions/runs/9"})
    assert "Requested publication of v0.21.5." in published
    assert "Workflow: https://github.com/example/hermes-agent/actions/runs/9" in published
    assert "moves the stable channel" in published

    abandoned = abandon_steps({"burned": "0.21.5", "tag": "v0.21.5-rc"})
    assert "Deleted the draft for v0.21.5." in abandoned
    assert "v0.21.5-rc" in abandoned
    assert "cannot be reused" in abandoned


def test_publish_dispatches_the_sequencer_and_abandon_keeps_the_claim(source):
    from scripts.releases.entrypoint import ReleaseRefused, abandon, publish

    commit = git(source, "rev-parse", "HEAD")
    _claim(source, "0.21.5", commit)
    calls = []
    published = publish("0.21.5", repository="example/hermes-agent", dispatch=calls.append)
    assert published["requested"] == "v0.21.5"
    assert published["version"] == "0.21.5"
    assert published["repository"] == "example/hermes-agent"
    assert calls == [[
        "gh", "workflow", "run", "stable-release-publication.yml",
        "--repo", "example/hermes-agent", "--raw-field", "version=0.21.5",
    ]]

    def inspect(command):
        if "v0.21.5" in command:
            return json.dumps({"tagName": "v0.21.5", "isDraft": True, "isPrerelease": False})
        raise ReleaseRefused("not found")

    abandoned = abandon("0.21.5", repo=source, repository="example/hermes-agent",
                        delete=calls.append, inspect=inspect)
    assert abandoned["burned"] == "0.21.5"
    assert abandoned["tag"] == "v0.21.5"
    assert calls[-1] == ["gh", "release", "delete", "v0.21.5", "--repo", "example/hermes-agent", "--yes"]
    assert "v0.21.5-rc" in git(source, "tag", "--list")

    with pytest.raises(ReleaseRefused, match="burned or superseded by 0\\.21\\.6"):
        publish(
            "0.21.5", repository="example/hermes-agent",
            dispatch=lambda _command: pytest.fail("superseded publish must not dispatch"),
            inspect=lambda _command: pytest.fail("superseded publish must not inspect drafts"),
            head_version=lambda: "0.21.6",
        )


def test_concurrent_claim_loser_reports_the_remote_winner_and_the_version_stays_spent(
        source, tmp_path, monkeypatch):
    from scripts.releases import entrypoint
    from scripts.releases.versioning import derive_next_version

    old = git(source, "rev-parse", "HEAD")
    git(source, "commit", "--allow-empty", "--quiet", "-m", "later")
    git(source, "push", "--quiet", "origin", "main")
    new = git(source, "rev-parse", "HEAD")
    origin = git(source, "remote", "get-url", "origin")
    left, right = tmp_path / "left", tmp_path / "right"
    git(tmp_path, "clone", "--quiet", origin, str(left))
    git(tmp_path, "clone", "--quiet", origin, str(right))
    for clone in (left, right):
        git(clone, "config", "user.name", "Test")
        git(clone, "config", "user.email", "test@example.test")
    git(left, "checkout", "--quiet", old)

    barrier = threading.Barrier(2)
    winner_pushed = threading.Event()
    original_git = entrypoint._git

    def racing_git(repo, *args):
        if args[:2] == ("push", "origin") and args[-1] == "refs/tags/v0.21.5-rc":
            barrier.wait(timeout=10)
            if repo == left:
                if not winner_pushed.wait(timeout=10):
                    raise TimeoutError("winning claim did not finish")
            else:
                try:
                    return original_git(repo, *args)
                finally:
                    winner_pushed.set()
        return original_git(repo, *args)

    monkeypatch.setattr(entrypoint, "_git", racing_git)
    outcomes = {}

    def claim(name, repo, commit):
        try:
            outcomes[name] = entrypoint.release(
                commit, bump="patch", repo=repo, remote="origin",
                repository="example/hermes-agent", execute=lambda _command: None,
            )
        except Exception as error:
            outcomes[name] = error

    threads = [
        threading.Thread(target=claim, args=("old", left, old)),
        threading.Thread(target=claim, args=("new", right, new)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert outcomes["new"]["commit"] == new
    assert isinstance(outcomes["old"], entrypoint.ReleaseRefused)
    assert "was claimed by Test" in str(outcomes["old"])
    assert new in str(outcomes["old"])
    assert git(left, "rev-parse", "v0.21.5-rc^{commit}") == new

    fresh = tmp_path / "fresh"
    git(tmp_path, "clone", "--quiet", origin, str(fresh))
    claims = git(fresh, "tag", "--list", "v*-rc").splitlines()
    assert derive_next_version(published=None, claims=claims, bump="patch") == "0.21.6"
