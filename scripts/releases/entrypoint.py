"""The thin release entrypoint: claim a version, cut the draft, dispatch the gate.

Nothing here builds. The claim is an annotated ``-rc`` tag pushed as exactly
that ref, and a dispatch that never starts is an error — the claim stays,
because a burned version is never retried under the same number.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from scripts.releases.versioning import SEED, derive_next_version

WORKFLOW = "stable-release.yml"


class ReleaseRefused(RuntimeError):
    """The release cannot proceed, and nothing was silently skipped."""


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=repo, text=True, encoding="utf-8").strip()


def _claims(repo: Path) -> list[str]:
    listed = _git(repo, "tag", "--list", "v*-rc")
    return [tag for tag in listed.splitlines() if tag]


def _claim_commit(repo: Path, tag: str) -> str:
    return _git(repo, "rev-parse", f"{tag}^{{commit}}")


def _refresh_claims(repo: Path, remote: str) -> None:
    _git(
        repo, "fetch", remote,
        "+refs/heads/main:refs/remotes/hermes-release/main",
        "+refs/tags/v*-rc:refs/tags/v*-rc",
    )


def _require_remote_main(repo: Path, commit: str) -> None:
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", commit, "refs/remotes/hermes-release/main"],
        cwd=repo, capture_output=True,
    )
    if result.returncode != 0:
        raise ReleaseRefused(f"{commit} is not on origin/main")


def _claim_collision(repo: Path, remote: str, tag: str, error: Exception) -> ReleaseRefused:
    subprocess.run(["git", "tag", "--delete", tag], cwd=repo, capture_output=True)
    try:
        _git(repo, "fetch", remote, f"+refs/tags/{tag}:refs/tags/{tag}")
        details = _git(
            repo, "for-each-ref", f"refs/tags/{tag}",
            "--format=%(taggername)|%(taggerdate:iso-strict)|%(*objectname)",
        )
    except subprocess.CalledProcessError:
        return ReleaseRefused(f"claim {tag} could not be pushed: {error}")
    actor, when, commit = details.split("|", 2)
    return ReleaseRefused(f"{tag} was claimed by {actor} at {when} for {commit}")


def _highest_claim(repo: Path) -> tuple[str, str] | None:
    """The highest-version outstanding claim, as (version, commit)."""
    from scripts.releases.versioning import version_from_tag

    best: tuple[list[int], str, str] | None = None
    for tag in _claims(repo):
        version = version_from_tag(tag[:-3]) if tag.endswith("-rc") else None
        if version is None:
            continue
        key = [int(part) for part in version.split(".")]
        if best is None or key > best[0]:
            best = (key, version, _claim_commit(repo, tag))
    return None if best is None else (best[1], best[2])


def _require_ancestry(repo: Path, commit: str) -> None:
    """A claim's commit must descend from the highest outstanding claim's."""
    highest = _highest_claim(repo)
    if highest is None:
        return
    version, claimed = highest
    ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", claimed, commit], cwd=repo, capture_output=True)
    if ancestor.returncode != 0:
        raise ReleaseRefused(
            f"{version} already claimed at {claimed} — publish or abandon it first")


def _next_claim_epoch(repo: Path) -> int:
    epochs = _git(repo, "for-each-ref", "refs/tags/v*-rc", "--format=%(taggerdate:unix)")
    previous = [int(value) for value in epochs.splitlines() if value.isdigit()]
    return max(int(time.time()), max(previous, default=0) + 1)


def release(commit: str, *, bump: str, repo: Path, remote: str, repository: str,
            execute, autopublish: bool = False, published: str = SEED) -> dict:
    """Claim the derived version, cut its draft, and start the gate."""
    _refresh_claims(repo, remote)
    _require_remote_main(repo, commit)
    _require_ancestry(repo, commit)
    version = derive_next_version(
        published=published, claims=_claims(repo), bump=bump,
    )
    tag = f"v{version}-rc"
    claim_epoch = _next_claim_epoch(repo)
    claim = json.dumps({
        "schema": 1,
        "version": version,
        "commit": commit,
        "autopublish": autopublish,
        "claimEpoch": claim_epoch,
    }, sort_keys=True, separators=(",", ":"))
    subprocess.check_output(
        ["git", "tag", "-a", tag, commit, "-m", claim], cwd=repo,
        text=True, encoding="utf-8",
        env={**os.environ, "GIT_COMMITTER_DATE": f"@{claim_epoch} +0000"},
    )
    try:
        _git(repo, "push", remote, f"refs/tags/{tag}")
    except subprocess.CalledProcessError as error:
        raise _claim_collision(repo, remote, tag, error) from error
    ref = f"refs/tags/{tag}"
    remote_ref = dict(line.split()[::-1] for line in _git(
        repo, "ls-remote", remote, ref, f"{ref}^{{}}",
    ).splitlines())
    if (remote_ref.get(ref) != _git(repo, "rev-parse", ref)
            or remote_ref.get(f"{ref}^{{}}") != commit):
        raise ReleaseRefused(f"claim {tag} did not persist with exact remote custody")
    url = f"https://github.com/{repository}/releases/tag/{tag}"
    try:
        execute([
            "gh", "release", "create", tag, "--repo", repository,
            "--verify-tag", "--draft", "--generate-notes", "--title", f"Hermes Agent v{version}",
        ])
        execute([
            "gh", "workflow", "run", WORKFLOW, "--ref", tag, "--repo", repository,
            "--raw-field", f"tag={tag}",
        ])
        found = execute([
            "gh", "run", "list", "--repo", repository, "--workflow", WORKFLOW,
            "--branch", tag, "--json", "databaseId,url,headBranch,status",
        ])
    except Exception as exc:
        raise ReleaseRefused(f"release {tag} never started: {exc}") from exc
    run_url = _dispatched_run(found, tag)
    return {"version": version, "tag": tag, "commit": commit, "url": url,
            "final_url": f"https://github.com/{repository}/releases/tag/v{version}",
            "run_url": run_url, "autopublish": autopublish}


def _dispatched_run(raw: str, tag: str) -> str:
    """The run URL for the claim ref. Empty when the list has no such run yet."""
    try:
        rows = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return ""
    matched = [row for row in rows if isinstance(row, dict) and row.get("headBranch") == tag and row.get("url")]
    if len(matched) != 1:
        return ""
    return str(matched[0]["url"])


def _latest_run(raw: str) -> str:
    """The URL of the newest listed run. Empty when the list names none."""
    try:
        rows = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return ""
    for row in rows:
        if isinstance(row, dict) and row.get("url"):
            return str(row["url"])
    return ""


def _release_view(tag: str, repository: str, inspect) -> dict | None:
    try:
        return json.loads(inspect([
            "gh", "release", "view", tag, "--repo", repository,
            "--json", "tagName,isDraft,isPrerelease",
        ]))
    except ReleaseRefused as exc:
        if "not found" in str(exc).lower():
            return None
        raise


def _preflight_publish(version: str, repository: str, inspect, head_version) -> None:
    requested = tuple(map(int, version.split(".")))
    head = head_version()
    if head and tuple(map(int, head.split("."))) >= requested:
        raise ReleaseRefused(f"stable {version} is burned or superseded by {head}")
    rows = [row for tag in (f"v{version}", f"v{version}-rc")
            if (row := _release_view(tag, repository, inspect)) is not None]
    if len(rows) != 1:
        raise ReleaseRefused(f"stable {version} is burned or has no release draft")


def publish(version: str, *, repository: str, dispatch, inspect=None, head_version=None) -> dict:
    """Request ordered publication through the one production sequencer."""
    tag = f"v{version}"
    from hermes_cli.update_channel import STABLE_TAG_RE

    if not STABLE_TAG_RE.fullmatch(tag):
        raise ReleaseRefused(f"{version} is not a stable version")
    if inspect is not None and head_version is not None:
        _preflight_publish(version, repository, inspect, head_version)
    dispatch([
        "gh", "workflow", "run", "stable-release-publication.yml",
        "--repo", repository, "--raw-field", f"version={version}",
    ])
    return {"requested": tag, "version": version, "repository": repository}


def abandon(version: str, *, repo: Path, repository: str, delete, inspect=None) -> dict:
    """Delete the draft. The claim tag stays, so the version is spent."""
    tag = f"v{version}-rc"
    if inspect is not None:
        rows = [row for candidate in (f"v{version}", tag)
                if (row := _release_view(candidate, repository, inspect)) is not None]
        if len(rows) != 1 or rows[0].get("isDraft") is not True:
            raise ReleaseRefused(f"stable {version} has no single release draft to abandon")
        tag = rows[0]["tagName"]
    delete(["gh", "release", "delete", tag, "--repo", repository, "--yes"])
    return {"burned": version, "tag": tag, "repository": repository}


def next_steps(result: dict) -> str:
    """Say what started, what the operator waits for, and the next action."""
    version = result["version"]
    lines = [
        f"Claimed v{version}. The release workflow started on {result['tag']}.",
        f"Workflow: {result['run_url']}" if result.get("run_url") else "Workflow: the run is not listed yet. Open the Actions tab for this claim.",
        "Wait for that workflow to finish. It builds and tests this commit.",
        f"When it is green, the release notes are at {result['final_url']}.",
    ]
    if result["autopublish"]:
        lines.append("Autopublish is on. A green workflow publishes the release. You do not run publish.")
    else:
        lines.append("Autopublish is off. The release stays a draft.")
        lines.append("Edit the notes at that page, then publish the release to push this build live:")
        lines.append(f"    python scripts/release.py publish --version {version} --remote origin")
    return "\n".join(lines)


def cmd_release(args) -> None:
    """The ``release`` subcommand: claim, draft, dispatch."""
    from scripts import release as release_script

    repo = release_script.REPO_ROOT
    remote = release_script.resolve_push_remote(args.remote)
    repository = release_script.remote_github_repo(remote)
    if not repository:
        raise SystemExit(f"release: remote {remote!r} does not point at a GitHub repository")
    commit = _git(repo, "rev-parse", "--verify", f"{args.commit}^{{commit}}")

    def execute(command: list[str]) -> None:
        completed = subprocess.run(command, cwd=repo, capture_output=True, text=True, encoding="utf-8")
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or "release command failed")
        return completed.stdout

    from scripts.releases.versioning import published_stable_version
    result = release(
        commit, bump=args.bump, repo=repo, remote=remote, repository=repository,
        execute=execute, autopublish=args.autopublish,
        published=published_stable_version(repository),
    )
    print(result["url"])
    print(next_steps(result))


def _command_repository(args) -> tuple[Path, str]:
    from scripts import release as release_script

    repo = release_script.REPO_ROOT
    remote = release_script.resolve_push_remote(args.remote)
    repository = release_script.remote_github_repo(remote)
    if not repository:
        raise SystemExit(f"release: remote {remote!r} does not point at a GitHub repository")
    return repo, repository


def _execute(repo: Path, command: list[str]) -> None:
    completed = subprocess.run(command, cwd=repo, capture_output=True, text=True, encoding="utf-8")
    if completed.returncode != 0:
        raise ReleaseRefused(completed.stderr.strip() or "release command failed")


def _inspect(repo: Path, command: list[str]) -> str:
    completed = subprocess.run(command, cwd=repo, capture_output=True, text=True, encoding="utf-8")
    if completed.returncode != 0:
        raise ReleaseRefused(completed.stderr.strip() or "release inspection failed")
    return completed.stdout


def publish_steps(result: dict) -> str:
    """Say that publication was requested, and where to watch it."""
    version = result["version"]
    lines = [
        f"Requested publication of v{version}.",
        f"Workflow: {result['run_url']}" if result.get("run_url") else "Workflow: the run is not listed yet. Open the Actions tab.",
        "Wait for that workflow to finish. It publishes the draft and moves the stable channel.",
        f"The release page is https://github.com/{result['repository']}/releases/tag/v{version}.",
    ]
    return "\n".join(lines)


def abandon_steps(result: dict) -> str:
    """Say that the draft is gone and the version cannot be reused."""
    version = result["burned"]
    return "\n".join([
        f"Deleted the draft for v{version}.",
        f"The claim tag {result['tag']} stays, so v{version} is spent.",
        "The next release takes the next version. This one cannot be reused.",
    ])


def cmd_publish(args) -> None:
    repo, repository = _command_repository(args)
    from scripts.releases.versioning import published_stable_version
    result = publish(args.version, repository=repository,
                     dispatch=lambda command: _execute(repo, command),
                     inspect=lambda command: _inspect(repo, command),
                     head_version=lambda: published_stable_version(repository))
    listed = _inspect(repo, [
        "gh", "run", "list", "--repo", repository, "--workflow", "stable-release-publication.yml",
        "--json", "databaseId,url,headBranch,status", "--limit", "1",
    ])
    print(publish_steps({**result, "run_url": _latest_run(listed)}))


def cmd_abandon(args) -> None:
    repo, repository = _command_repository(args)
    result = abandon(args.version, repo=repo, repository=repository,
                     delete=lambda command: _execute(repo, command),
                     inspect=lambda command: _inspect(repo, command))
    print(abandon_steps(result))
