"""Stable release admission, signed-package transitions and final release receipt."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tomllib
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit

from hermes_cli.update_channel import STABLE_TAG_RE
SHA = re.compile(r"[a-f0-9]{40}")
DIGEST = re.compile(r"[a-f0-9]{64}")
DESKTOP_TARGETS = ("windows/x64", "windows/arm64", "macos/x64", "macos/arm64")
SMOKE_JOBS = {
    "smoke-darwin": "macOS DMG + ZIP (arm64 and x64)",
    "smoke-win32": "Windows MSIX (arm64 and x64)",
    "smoke-win32-universal": "Windows MSIXBUNDLE (arm64 and x64)",
}


def admit_claim(tag: str, commit: str, *, on_main) -> dict:
    """Admit a release from its claim tag. The checkout version is not read.

    ``main`` carries ``0.0.0`` on purpose, so the version comes from the
    ``-rc`` tag and the only question about the commit is whether it is on
    ``main``.
    """
    from scripts.releases.versioning import version_from_tag

    if not isinstance(tag, str) or not tag.endswith("-rc"):
        raise ValueError(f"{tag} is not a claim tag")
    version = version_from_tag(tag[:-3])
    if version is None:
        raise ValueError(f"{tag} is not a claim tag")
    if not on_main(commit):
        raise ValueError(f"{commit} is not on main")
    return {"claim_tag": tag, "tag": f"v{version}", "version": version, "commit": commit}


def require_stable_identity(tag: str, commit: str) -> None:
    """Validate the final payload identity without requiring its future ref."""
    if not isinstance(tag, str) or not STABLE_TAG_RE.fullmatch(tag) or not SHA.fullmatch(commit or ""):
        raise ValueError("Invalid stable payload identity")


def require_success(needs: dict, required: list[str]) -> None:
    if not required or len(set(required)) != len(required):
        raise ValueError("Invalid required-job list")
    failures = [f"{name}={needs.get(name, {}).get('result', 'missing')}"
                for name in required if needs.get(name, {}).get("result") != "success"]
    if failures:
        raise ValueError("Release blocked: " + ", ".join(failures))


def successful_smoke_results(needs: object) -> dict:
    """Persist only observed successful native groups, never infer them from artifacts."""
    if not isinstance(needs, dict):
        raise ValueError("Candidate smoke results must be a job-result object")
    results = {}
    for job in SMOKE_JOBS:
        row = needs.get(job)
        results[job] = {"result": row.get("result") if isinstance(row, dict) else None}
    require_success(results, list(SMOKE_JOBS))
    return results


def stable_windows_version(epoch: object) -> str:
    if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 0:
        raise ValueError("Stable release epoch must be a non-negative integer")
    instant = datetime.fromtimestamp(epoch, tz=timezone.utc)
    start = datetime(instant.year, 1, 1, tzinfo=timezone.utc)
    hour_of_year = (instant - start).days * 24 + instant.hour
    second_of_hour = instant.minute * 60 + instant.second
    return f"{instant.year}.{hour_of_year}.{second_of_hour}.0"


def validate_candidates(manifest: dict, tag: str, commit: str, public_base: str,
                        release_epoch: int | None = None) -> dict:
    require_stable_identity(tag, commit)
    if manifest.get("schema") != 2 or manifest.get("tag") != tag or manifest.get("commit") != commit or not isinstance(manifest.get("packages"), list):
        raise ValueError("Candidate manifest does not match release identity")
    successful_smoke_results(manifest.get("smoke_results"))
    admitted_epoch = manifest.get("releaseEpoch")
    expected_windows_version = stable_windows_version(admitted_epoch)
    if release_epoch is not None and admitted_epoch != release_epoch:
        raise ValueError("Candidate release epoch differs from the admitted claim")
    prefix = urlsplit(f"{public_base.rstrip('/')}/releases/tag/{tag}/")
    if prefix.scheme != "https" or prefix.username or prefix.password or not prefix.netloc:
        raise ValueError("Public release origin must use HTTPS")
    rows = {}
    for item in manifest["packages"]:
        target = f"{item.get('platform')}/{item.get('arch')}"
        if target not in (*DESKTOP_TARGETS, "termux/aarch64") or target in rows or item.get("tag") != tag or item.get("commit") != commit:
            raise ValueError(f"Invalid or duplicate candidate target: {target}")
        artifact = item.get("artifact", {})
        url = urlsplit(artifact.get("url", ""))
        decoded = unquote(url.path)
        if any(part in (".", "..") for part in decoded.split("/")) or "\\" in decoded or "%" in decoded:
            raise ValueError("Invalid artifact path encoding")
        if (url.scheme, url.netloc) != (prefix.scheme, prefix.netloc) or not url.path.startswith(prefix.path) or url.query or url.fragment or url.username or url.password:
            raise ValueError(f"Candidate package is outside its immutable tag archive: {target}")
        if not DIGEST.fullmatch(artifact.get("sha256", "")) or not item.get("identity"):
            raise ValueError(f"Invalid candidate digest or identity: {target}")
        if item["platform"] == "windows":
            if (item.get("version") != expected_windows_version
                    or item.get("executableVersion") != expected_windows_version):
                raise ValueError("Windows candidate version differs from the admitted release epoch")
            windows_version(item["version"])
            if not item.get("publisher") or not item.get("applicationId") or not url.path.endswith(".msixbundle"):
                raise ValueError("Windows candidate needs publisher, applicationId and MSIX bundle")
        elif item["platform"] == "macos":
            if item.get("version") != tag[1:] or not re.fullmatch(r"[A-Z0-9]{10}", item.get("teamId", "")) or not url.path.endswith(".zip"):
                raise ValueError("macOS candidate needs matching version, signing team and app ZIP")
        elif item.get("version") != f"{tag[1:]}-1":
            raise ValueError("Termux candidate version differs from the admitted release")
        rows[target] = item
    if any(target not in rows for target in DESKTOP_TARGETS):
        raise ValueError("Candidate manifest must cover Windows and macOS on both architectures")
    return rows


def windows_version(value: str) -> tuple[int, ...]:
    if not isinstance(value, str) or not re.fullmatch(r"\d+\.\d+\.\d+\.\d+", value):
        raise ValueError("Windows package version must have four numeric components")
    result = tuple(map(int, value.split(".")))
    if any(n > 65535 for n in result):
        raise ValueError("Windows package version exceeds 16 bits")
    return result


def plan_transitions(previous: dict, candidate: dict, public_base: str) -> list[dict]:
    old = validate_candidates(previous, previous.get("tag"), previous.get("commit"), public_base)
    new = validate_candidates(candidate, candidate.get("tag"), candidate.get("commit"), public_base)
    result = []
    for target in DESKTOP_TARGETS:
        left, right = old[target], new[target]
        if left["identity"] != right["identity"] or left["commit"] == right["commit"] or left["artifact"]["sha256"] == right["artifact"]["sha256"]:
            raise ValueError("Update must preserve package identity and change the build")
        if right["platform"] == "windows":
            if (left["publisher"], left["applicationId"]) != (right["publisher"], right["applicationId"]):
                raise ValueError("Update must preserve publisher and applicationId")
            newer = windows_version(right["version"]) > windows_version(left["version"])
        else:
            if left["teamId"] != right["teamId"]:
                raise ValueError("Update must preserve signing team")
            newer = tuple(map(int, right["version"].split("."))) > tuple(map(int, left["version"].split(".")))
        if not newer:
            raise ValueError("New package version must increase")
        result.append({"target": target.replace("/", "-"), "transition": {
            "schema": 1, "platform": right["platform"], "arch": right["arch"], "old": left, "new": right,
        }})
    return result


def read_manifest(url: str, expected_hash: str | None = None, *, expected_origin: str | None = None,
                  opener=urllib.request.urlopen) -> dict:
    location = urlsplit(url)
    origin = urlsplit(expected_origin or url)

    def check_origin(target):
        if target.scheme != "https" or not target.hostname or target.username or target.password:
            raise ValueError("Manifest origin must use HTTPS without credentials")
        if (target.scheme, target.hostname, target.port or 443) != (origin.scheme, origin.hostname, origin.port or 443):
            raise ValueError("Manifest is outside the expected release origin")

    check_origin(location)
    with opener(url, timeout=60) as response:
        check_origin(urlsplit(response.geturl()))
        data = response.read(1024 * 1024 + 1)
    if len(data) > 1024 * 1024:
        raise ValueError("Release manifest exceeds size limit")
    if expected_hash and hashlib.sha256(data).hexdigest() != expected_hash:
        raise ValueError("Candidate manifest digest mismatch")
    return json.loads(data)


def output(argv: list[str]) -> str:
    return subprocess.check_output(argv, text=True, encoding="utf-8").strip()


def _claim_metadata(raw: str, *, version: str, commit: str) -> dict:
    try:
        metadata = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError("Stable claim metadata is invalid") from error
    expected = {"schema": 1, "version": version, "commit": commit}
    if (not isinstance(metadata, dict)
            or any(metadata.get(key) != value for key, value in expected.items())
            or not isinstance(metadata.get("autopublish"), bool)
            or not isinstance(metadata.get("claimEpoch"), int)
            or metadata["claimEpoch"] <= 0
            or set(metadata) != {*expected, "autopublish", "claimEpoch"}):
        raise ValueError("Stable claim metadata is invalid")
    return metadata


def tagger_epoch(tag_object: str, run=output) -> int:
    body = run(["git", "cat-file", "-p", tag_object])
    tagger = next((line for line in body.splitlines() if line.startswith("tagger ")), None)
    if tagger is None:
        raise ValueError("Stable claim has no tagger timestamp")
    try:
        epoch = int(tagger.rsplit(" ", 2)[1])
    except (IndexError, ValueError) as error:
        raise ValueError("Stable claim tagger timestamp is invalid") from error
    if epoch <= 0:
        raise ValueError("Stable claim tagger timestamp is invalid")
    return epoch


def check_claim(env: dict, run=output) -> dict:
    """Bind the run to one remote annotated claim object and its commit."""
    claim_tag, commit = env.get("RELEASE_CLAIM_TAG"), env.get("GITHUB_SHA")
    if not isinstance(claim_tag, str) or env.get("GITHUB_REF") != f"refs/tags/{claim_tag}":
        raise ValueError("Stable release must run on its exact claim ref")
    if not isinstance(commit, str) or not SHA.fullmatch(commit):
        raise ValueError("Stable claim needs an exact commit")
    claim_ref = f"refs/tags/{claim_tag}"
    local_object = run(["git", "rev-parse", claim_ref])
    local_commit = run(["git", "rev-parse", f"{claim_ref}^{{commit}}"])
    if run(["git", "cat-file", "-t", local_object]) != "tag":
        raise ValueError("Stable claim must be an annotated tag")
    remote = dict(line.split()[::-1] for line in run(
        ["git", "ls-remote", "origin", claim_ref, f"{claim_ref}^{{}}"]
    ).splitlines())
    remote_object = remote.get(claim_ref)
    remote_commit = remote.get(f"{claim_ref}^{{}}")
    expected_object = env.get("RELEASE_CLAIM_OBJECT")
    if (local_commit != commit or remote_commit != commit or remote_object != local_object
            or (expected_object and remote_object != expected_object)
            or run(["git", "rev-parse", "HEAD"]) != commit):
        raise ValueError("Stable claim tag or checkout moved")
    run(["git", "fetch", "origin", "+refs/heads/main:refs/remotes/origin/main"])

    def on_main(sha: str) -> bool:
        try:
            run(["git", "merge-base", "--is-ancestor", sha, "origin/main"])
        except subprocess.CalledProcessError:
            return False
        return True

    admitted = admit_claim(claim_tag, commit, on_main=on_main)
    raw_metadata = run(["git", "tag", "-l", claim_tag, "--format=%(contents)"])
    metadata = _claim_metadata(raw_metadata, version=admitted["version"], commit=commit)
    claim_epoch = tagger_epoch(local_object, run)
    if metadata["claimEpoch"] != claim_epoch:
        raise ValueError("Stable claim epoch differs from its annotated tagger timestamp")
    return {**admitted, "claim_object": local_object,
            "autopublish": metadata["autopublish"], "claim_epoch": claim_epoch}


def stable_context(env: dict, run=output) -> tuple[str, str, dict]:
    claim = check_claim(env, run=run)
    tag = env.get("RELEASE_TAG")
    if not isinstance(tag, str) or tag != claim["tag"]:
        raise ValueError("Stable payload tag differs from the admitted claim")
    return tag, claim["commit"], claim


def final_context(env: dict, run=output) -> tuple[str, str, dict]:
    """Verify the final annotated receipt, its claim, and published release."""
    repository = env.get("GITHUB_REPOSITORY", "")
    tag = env.get("RELEASE_TAG", "")
    commit = env.get("RELEASE_COMMIT", "")
    claim_tag = env.get("RELEASE_CLAIM_TAG", "")
    claim_object = env.get("RELEASE_CLAIM_OBJECT", "")
    require_stable_identity(tag, commit)
    admitted = admit_claim(claim_tag, commit, on_main=lambda _commit: True)
    if admitted["tag"] != tag or not SHA.fullmatch(claim_object):
        raise ValueError("Final release differs from its claim")

    refs = {}
    for line in run(["git", "ls-remote", "origin",
                     f"refs/tags/{claim_tag}", f"refs/tags/{claim_tag}^{{}}",
                     f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}"]).splitlines():
        sha, ref = line.split()
        refs[ref] = sha
    if (refs.get(f"refs/tags/{claim_tag}") != claim_object
            or refs.get(f"refs/tags/{claim_tag}^{{}}") != commit
            or refs.get(f"refs/tags/{tag}^{{}}") != commit
            or not refs.get(f"refs/tags/{tag}")):
        raise ValueError("Final release tag custody changed")

    run(["git", "fetch", "origin", "+refs/heads/main:refs/remotes/origin/main",
         f"+refs/tags/{claim_tag}:refs/tags/{claim_tag}",
         f"+refs/tags/{tag}:refs/tags/{tag}"])
    for receipt, expected_object in ((claim_tag, claim_object),
                                     (tag, refs[f"refs/tags/{tag}"])):
        local_object = run(["git", "rev-parse", f"refs/tags/{receipt}"])
        if local_object != expected_object or run(["git", "cat-file", "-t", local_object]) != "tag":
            raise ValueError("Final release local tag differs from the remote")
    run(["git", "merge-base", "--is-ancestor", commit, "origin/main"])
    claim = _claim_metadata(
        run(["git", "tag", "-l", claim_tag, "--format=%(contents)"]),
        version=admitted["version"], commit=commit,
    )
    final = json.loads(run(["git", "tag", "-l", tag, "--format=%(contents)"]))
    expected = {
        "schema": 1, "version": admitted["version"], "commit": commit,
        "claimTag": claim_tag, "claimTagObject": claim_object,
        "autopublish": claim["autopublish"],
        "claimEpoch": claim["claimEpoch"],
        "releaseId": final.get("releaseId"),
        "candidateManifestSha256": final.get("candidateManifestSha256"),
        "dockerManifestDigest": final.get("dockerManifestDigest"),
    }
    if (final != expected
            or not isinstance(final["releaseId"], int) or final["releaseId"] <= 0
            or not DIGEST.fullmatch(final["candidateManifestSha256"] or "")
            or not re.fullmatch(r"sha256:[a-f0-9]{64}", final["dockerManifestDigest"] or "")):
        raise ValueError("Final tag metadata differs from its claim")
    release = json.loads(run([
        "gh", "api", f"repos/{repository}/releases/tags/{tag}",
    ]))
    if (release.get("id") != final["releaseId"] or release.get("tag_name") != tag
            or release.get("draft") is not False
            or release.get("prerelease") is not False or not release.get("published_at")):
        raise ValueError("Stable channel requires the published final release")
    return tag, commit, {**admitted, "claim_object": claim_object,
                         "autopublish": claim["autopublish"],
                         "claim_epoch": claim["claimEpoch"],
                         "release_id": final["releaseId"],
                         "candidate_manifest_sha256": final["candidateManifestSha256"],
                         "docker_manifest_digest": final["dockerManifestDigest"]}


def emit(values: dict, env: dict) -> None:
    with Path(env["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as file:
        for key, value in values.items():
            file.write(f"{key}={value if isinstance(value, str) else json.dumps(value, separators=(',', ':'))}\n")


def read_candidate(env: dict) -> dict:
    digest = env.get("CANDIDATE_MANIFEST_SHA256", "")
    if not DIGEST.fullmatch(digest):
        raise ValueError("Pinned candidate manifest digest is required")
    return read_manifest(env["CANDIDATE_MANIFEST_URL"], digest)


def read_admitted_candidate(tag: str, commit: str, public_base: str, digest: str) -> dict:
    """The page and package promoter consume the same pinned admission."""
    if not DIGEST.fullmatch(digest or ""):
        raise ValueError("Pinned candidate manifest digest is required")
    require_stable_identity(tag, commit)
    manifest = read_manifest(f"{public_base.rstrip('/')}/releases/tag/{tag}/release-candidates.json",
                             digest, expected_origin=public_base)
    validate_candidates(manifest, tag, commit, public_base)
    return manifest


def summary(text: str, env: dict) -> None:
    with Path(env["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as file:
        file.write(text + "\n")


def admit(env: dict) -> None:
    """Admit the claim. The checkout carries 0.0.0, so the tag is the version."""
    admitted = check_claim(env)
    repository = env["GITHUB_REPOSITORY"]
    release = json.loads(output([
        "gh", "release", "view", admitted["claim_tag"], "--repo", repository,
        "--json", "databaseId,tagName,isDraft,isPrerelease",
    ]))
    if (release.get("tagName") != admitted["claim_tag"] or release.get("isDraft") is not True
            or release.get("isPrerelease") is not False or not isinstance(release.get("databaseId"), int)):
        raise ValueError("Stable claim must already own one non-prerelease draft")
    emit({
        "claim-tag": admitted["claim_tag"], "claim-object": admitted["claim_object"],
        "tag": admitted["tag"], "commit": admitted["commit"], "version": admitted["version"],
        "release-id": release["databaseId"], "release-epoch": admitted["claim_epoch"],
    }, env)
    summary(
        f"## Stable candidate {admitted['claim_tag']}\nCommit: {admitted['commit']}\n"
        f"Version: {admitted['version']}\nPayload tag: {admitted['tag']}\n",
        env,
    )


def verify(env: dict) -> None:
    """Revalidate claim custody in a reusable privileged workflow."""
    tag, commit, claim = stable_context(env)
    emit({"tag": tag, "sha": commit, "channel": "stable", "payload-version": tag[1:],
          "release-epoch": claim["claim_epoch"]}, env)


def transitions(env: dict) -> None:
    from scripts.releases.r2 import put

    tag, commit, claim = stable_context(env)
    base = env["CLOUDFLARE_R2_PUBLIC_URL"].rstrip("/")
    candidate = read_candidate(env)
    validate_candidates(candidate, tag, commit, base, claim["claim_epoch"])
    try:
        previous = read_manifest(env.get("BASELINE_MANIFEST_URL") or f"{base}/releases/stable/release-candidates.json",
                                 expected_origin=base)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise ValueError("No published stable package baseline. Supply baseline-manifest for an actual previous stable release; acceptance cannot be skipped.") from error
        raise
    published = json.loads(output(["gh", "release", "view", previous["tag"], "--repo", env["GITHUB_REPOSITORY"], "--json", "tagName,isDraft,isPrerelease"]))
    if published["tagName"] != previous["tag"] or published["isDraft"] or published["isPrerelease"]:
        raise ValueError("Upgrade baseline must be a published stable release")
    matrices = {"windows": {"include": []}, "macos": {"include": []}}
    for row in plan_transitions(previous, candidate, base):
        transition = row["transition"]
        name = f"acceptance-{row['target']}.json"
        file = Path(env["RUNNER_TEMP"]) / name
        file.write_text(json.dumps(transition), encoding="utf-8")
        put(tag=tag, key=name, file=file, immutable=True)
        url = f"{base}/releases/tag/{tag}/{name}"
        if read_manifest(url) != transition:
            raise ValueError("Transition manifest read-back mismatch")
        matrices[transition["platform"]]["include"].append({"arch": transition["arch"], "manifest": url, "old": transition["old"]["tag"], "id": row["target"], "manifest_sha256": hashlib.sha256(file.read_bytes()).hexdigest()})
    emit(matrices, env)


def _final_metadata(tag: str, commit: str, claim: dict, candidate_manifest_sha256: str,
                    docker_manifest_digest: str, release_id: int) -> dict:
    if not DIGEST.fullmatch(candidate_manifest_sha256):
        raise ValueError("Final tag candidate manifest digest is invalid")
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", docker_manifest_digest):
        raise ValueError("Final tag Docker manifest digest is invalid")
    if not isinstance(release_id, int) or release_id <= 0:
        raise ValueError("Final tag release database ID is invalid")
    return {
        "schema": 1, "version": tag[1:], "commit": commit,
        "claimTag": claim["claim_tag"], "claimTagObject": claim["claim_object"],
        "autopublish": claim["autopublish"],
        "claimEpoch": claim["claim_epoch"],
        "releaseId": release_id,
        "candidateManifestSha256": candidate_manifest_sha256,
        "dockerManifestDigest": docker_manifest_digest,
    }


def ensure_final_tag(tag: str, commit: str, claim: dict, *, candidate_manifest_sha256: str,
                     docker_manifest_digest: str, release_id: int, run=output) -> str:
    """Create or verify the immutable annotated final tag."""
    require_stable_identity(tag, commit)
    expected = _final_metadata(
        tag, commit, claim, candidate_manifest_sha256, docker_manifest_digest, release_id,
    )
    ref = f"refs/tags/{tag}"
    remote_raw = run(["git", "ls-remote", "origin", ref, f"{ref}^{{}}"])
    if not remote_raw:
        try:
            local_object = run(["git", "rev-parse", "--verify", ref])
        except subprocess.CalledProcessError:
            message = json.dumps(expected, sort_keys=True, separators=(",", ":"))
            run([
                "git", "-c", "user.name=Hermes Release Automation",
                "-c", "user.email=release-bot@users.noreply.github.com",
                "tag", "-a", tag, commit, "-m", message,
            ])
        else:
            if (run(["git", "cat-file", "-t", local_object]) != "tag"
                    or run(["git", "rev-parse", f"{ref}^{{commit}}"]) != commit
                    or json.loads(run(["git", "tag", "-l", tag, "--format=%(contents)"])) != expected):
                raise ValueError("Local final tag collision")
        run(["git", "push", "origin", ref])
        remote_raw = run(["git", "ls-remote", "origin", ref, f"{ref}^{{}}"])
    remote = dict(line.split()[::-1] for line in remote_raw.splitlines())
    tag_object, peeled = remote.get(ref), remote.get(f"{ref}^{{}}")
    if not tag_object or peeled != commit:
        raise ValueError("Final stable tag points at the wrong commit or is lightweight")
    try:
        local_object = run(["git", "rev-parse", ref])
    except subprocess.CalledProcessError:
        run(["git", "fetch", "origin", f"{ref}:{ref}"])
        local_object = run(["git", "rev-parse", ref])
    if local_object != tag_object or run(["git", "cat-file", "-t", local_object]) != "tag":
        raise ValueError("Final stable tag object differs from the verified remote")
    metadata = json.loads(run(["git", "tag", "-l", tag, "--format=%(contents)"]))
    if metadata != expected:
        raise ValueError("Final stable tag metadata differs from the accepted artifacts")
    return tag_object


def retarget_release(repository: str, release_id: int, tag: str, commit: str, *, publish: bool,
                     run=output) -> None:
    endpoint = f"repos/{repository}/releases/{release_id}"
    current = json.loads(run(["gh", "api", endpoint]))
    if (current.get("id") == release_id and current.get("tag_name") == tag
            and current.get("prerelease") is False and current.get("draft") is False):
        return
    run([
        "gh", "api", "--method", "PATCH", endpoint,
        "--raw-field", f"tag_name={tag}", "--raw-field", f"target_commitish={commit}",
        "--field", "prerelease=false", "--raw-field", "make_latest=true",
        "--field", f"draft={str(not publish).lower()}",
    ])
    release = json.loads(run(["gh", "api", endpoint]))
    if (release.get("id") != release_id or release.get("tag_name") != tag
            or release.get("prerelease") is not False or release.get("draft") is not (not publish)):
        raise ValueError("Stable release retarget did not persist")


def complete(env: dict) -> None:
    tag, commit, claim = stable_context(env)
    base = env["CLOUDFLARE_R2_PUBLIC_URL"].rstrip("/")
    candidate = read_candidate(env)
    validate_candidates(candidate, tag, commit, base, claim["claim_epoch"])
    release_id = env.get("RELEASE_ID", "")
    if not str(release_id).isdigit():
        raise ValueError("Stable release database ID is required")
    ensure_final_tag(
        tag, commit, claim,
        candidate_manifest_sha256=env["CANDIDATE_MANIFEST_SHA256"],
        docker_manifest_digest=env.get("DOCKER_MANIFEST_DIGEST", ""),
        release_id=int(release_id),
    )
    retarget_release(env["GITHUB_REPOSITORY"], int(release_id), tag, commit, publish=False)


def main(argv: list[str] | None = None, env: dict | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    env = os.environ if env is None else env
    if argv and argv[0] == "gate":
        needs = json.loads(env["RELEASE_NEEDS"])
        summary("\n".join(f"- {name}: {needs.get(name, {}).get('result', 'missing')}" for name in argv[1:]), env)
        require_success(needs, argv[1:])
        return
    commands = {"admit": admit, "verify": verify, "transitions": transitions, "complete": complete}
    if len(argv) != 1 or argv[0] not in commands:
        raise ValueError("Expected admit, verify, gate, transitions or complete")
    commands[argv[0]](env)


if __name__ == "__main__":
    main()
