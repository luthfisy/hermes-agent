"""Stable publication is ordered, held by default, and idempotent.

A green draft publishes when it opted into autopublish, or when a later green
release needs it resolved. A running older claim blocks only the versions above
it; already-resolvable older green claims still make progress.
"""
import json

import pytest


def _claims(*rows):
    return [dict(version=version, state=state, autopublish=autopublish)
            for version, state, autopublish in rows]


def _flips(steps):
    return [step["flip"] for step in steps if "flip" in step]


def test_a_sole_green_draft_waits_without_autopublish():
    from scripts.releases.sequencer import plan

    assert plan(_claims(("0.21.5", "green", False)), head="0.21.4") == []


def test_autopublish_flips_the_current_green_release():
    from scripts.releases.sequencer import plan

    steps = plan(_claims(("0.21.5", "green", True)), head="0.21.4")
    assert _flips(steps) == ["0.21.5"]
    assert steps[-1] == {"advance": "0.21.5"}


def test_a_newer_green_release_flushes_the_older_waiting_draft():
    from scripts.releases.sequencer import plan

    steps = plan(_claims(
        ("0.21.5", "green", False),
        ("0.21.6", "green", False),
    ), head="0.21.4")

    assert _flips(steps) == ["0.21.5", "0.21.6"]
    assert steps == [
        {"flip": "0.21.5"}, {"advance": "0.21.5"},
        {"flip": "0.21.6"}, {"advance": "0.21.6"},
    ]


def test_a_running_newer_claim_does_not_flush_a_held_draft():
    from scripts.releases.sequencer import plan

    steps = plan(_claims(
        ("0.21.5", "green", False),
        ("0.21.6", "running", False),
        ("0.21.7", "green", True),
    ), head="0.21.4")

    assert steps == []
    assert plan(_claims(
        ("0.21.5", "running", False),
        ("0.21.6", "green", True),
    ), head="0.21.4") == []
    assert plan(_claims(
        ("0.21.5", "green", False),
        ("0.21.6", "published", False),
    ), head="0.21.4") == []


def test_unstarted_claim_waits_for_its_grace_period_before_burning():
    from datetime import datetime, timedelta, timezone

    from scripts.releases.sequencer import classify_runs

    claimed = datetime(2026, 9, 22, 1, 0, tzinfo=timezone.utc)
    assert classify_runs([], claimed_at=claimed, now=claimed + timedelta(minutes=59)) == (
        "running", None,
    )
    assert classify_runs([], claimed_at=claimed, now=claimed + timedelta(hours=1)) == (
        "burned", None,
    )


def test_failed_run_retries_twice_after_backoff_before_burning():
    from datetime import datetime, timezone

    from scripts.releases.sequencer import classify_runs, retry_due

    now = datetime(2026, 9, 22, 1, 30, tzinfo=timezone.utc)
    failed = {
        "id": 42, "status": "completed", "conclusion": "failure",
        "run_attempt": 1, "updated_at": "2026-09-22T01:14:59Z",
    }
    state, retry = classify_runs([failed])
    assert state == "running"
    assert retry is not None
    assert retry_due([{"version": "0.21.5", "state": state, "retry": retry}], now=now) == [{
        "version": "0.21.5", "run_id": 42, "attempt": 2,
    }]
    newer = dict(retry)
    newer["run_id"] = 43
    assert retry_due([
        {"version": "0.21.5", "state": state, "retry": retry},
        {"version": "0.21.6", "state": state, "retry": newer},
    ], now=now) == [{"version": "0.21.5", "run_id": 42, "attempt": 2}]
    failed["run_attempt"] = 2
    state, retry = classify_runs([failed])
    assert retry_due([{"version": "0.21.5", "state": state, "retry": retry}], now=now)[0]["attempt"] == 3
    failed["run_attempt"] = 3
    assert classify_runs([failed]) == ("burned", None)


def test_a_burned_claim_is_spent_and_skipped():
    from scripts.releases.sequencer import classify_final_release, plan

    assert classify_final_release("v0.21.5", "v0.21.5-rc", None) == ("burned", False)

    steps = plan(_claims(
        ("0.21.5", "burned", False),
        ("0.21.6", "green", True),
    ), head="0.21.4")

    assert _flips(steps) == ["0.21.6"]
    assert steps[-1] == {"advance": "0.21.6"}


def test_advances_each_published_version_and_ignores_a_later_burned_claim():
    from scripts.releases.sequencer import plan

    assert plan(_claims(
        ("0.21.5", "published", False),
        ("0.21.6", "published", False),
    ), head="0.21.4") == [
        {"advance": "0.21.5"},
        {"advance": "0.21.6"},
    ]
    assert plan(_claims(
        ("0.21.5", "green", False),
        ("0.21.6", "burned", False),
    ), head="0.21.4") == []


def test_explicit_publish_uses_the_same_oldest_first_plan():
    from scripts.releases.sequencer import plan

    assert plan(_claims(
        ("0.21.5", "green", False),
        ("0.21.6", "green", False),
    ), head="0.21.4", requested_version="0.21.6") == [
        {"flip": "0.21.5"},
        {"advance": "0.21.5"},
        {"flip": "0.21.6"},
        {"advance": "0.21.6"},
    ]


def test_published_history_at_or_below_the_head_is_an_idempotent_noop():
    from scripts.releases.sequencer import plan

    assert plan(_claims(
        ("0.21.4", "published", False),
        ("0.21.5", "green", False),
    ), head="0.21.4") == []


def test_an_unpublished_claim_below_the_head_is_refused():
    from scripts.releases.sequencer import plan

    with pytest.raises(ValueError, match="backwards"):
        plan(_claims(("0.21.4", "green", True)), head="0.21.5")


def test_reconcile_discovers_custody_flips_then_advances_oldest_first():
    from scripts.releases.sequencer import reconcile

    commit = "a" * 40
    tags = {}
    releases = []
    for index, version in enumerate(("0.21.5", "0.21.6"), start=1):
        claim_tag, tag = f"v{version}-rc", f"v{version}"
        claim_object, final_object = str(index) * 40, str(index + 2) * 40
        claim = {
            "schema": 1, "version": version, "commit": commit,
            "autopublish": False, "claimEpoch": 1_790_000_000 + index,
        }
        final = {
            **claim, "claimTag": claim_tag, "claimTagObject": claim_object,
            "releaseId": index,
            "candidateManifestSha256": "a" * 64,
            "dockerManifestDigest": "sha256:" + "b" * 64,
        }
        tags[claim_tag] = (claim_object, commit, claim)
        tags[tag] = (final_object, commit, final)
        releases.append({
            "id": index, "tag_name": tag, "draft": True, "prerelease": False,
            "published_at": None,
        })

    events = []

    def run(argv):
        if argv[:2] == ["git", "fetch"]:
            return ""
        if argv[:3] == ["git", "ls-remote", "--tags"]:
            return "\n".join(
                f"{sha}\trefs/tags/{tag}\n{target}\trefs/tags/{tag}^{{}}"
                for tag, (sha, target, _message) in tags.items()
            )
        if argv[:2] == ["git", "rev-parse"]:
            return tags[argv[-1].removeprefix("refs/tags/")][0]
        if argv[:3] == ["git", "cat-file", "-t"]:
            return "tag"
        if argv[:3] == ["git", "cat-file", "-p"]:
            epoch = next(message["claimEpoch"] for object_id, _target, message in tags.values()
                         if object_id == argv[3])
            return f"tagger Fixture <fixture@example.test> {epoch} +0000\n"
        if argv[:3] == ["git", "tag", "-l"]:
            return json.dumps(tags[argv[3]][2])
        if argv[:4] == ["gh", "api", "--paginate", "--slurp"]:
            if "/releases?" in argv[-1]:
                return json.dumps([releases])
            return json.dumps([{"workflow_runs": []}])
        if argv[:3] == ["gh", "api", "--method"]:
            release_id = int(argv[4].rsplit("/", 1)[1])
            release = next(row for row in releases if row["id"] == release_id)
            release.update(tag_name=f"v0.21.{4 + release_id}", draft=False,
                           prerelease=False, published_at="2026-09-22T01:00:00Z")
            events.append(("flip", release["tag_name"]))
            return "{}"
        if argv[:2] == ["gh", "api"] and "/releases/" in argv[2]:
            release_id = int(argv[2].rsplit("/", 1)[1])
            return json.dumps(next(row for row in releases if row["id"] == release_id))
        raise AssertionError(argv)

    head = ["0.21.4"]

    releases[0]["id"] = 99
    assert reconcile(
        {"GITHUB_REPOSITORY": "example/project"},
        run=run, read_head=lambda: head[0], advance_head=lambda _record: None,
    ) == []
    releases[0]["id"] = 1

    def advance(record):
        events.append(("advance", record["tag"]))
        head[0] = record["version"]

    steps = reconcile(
        {"GITHUB_REPOSITORY": "example/project", "REQUESTED_VERSION": "0.21.6"},
        run=run, read_head=lambda: head[0], advance_head=advance,
    )

    assert steps == [
        {"flip": "0.21.5"}, {"advance": "0.21.5"},
        {"flip": "0.21.6"}, {"advance": "0.21.6"},
    ]
    assert events == [
        ("flip", "v0.21.5"), ("advance", "v0.21.5"),
        ("flip", "v0.21.6"), ("advance", "v0.21.6"),
    ]
