"""Stable admission reads the version from the claim, not from the checkout.

``main`` carries ``0.0.0`` by design, so comparing the tag against
``pyproject.toml`` would refuse every release. The claim tag is the version,
and the only question about the commit is whether it is on ``main``.
"""
import pytest


def test_admission_reads_the_version_from_the_claim():
    from scripts.releases.stable import admit_claim

    commit = "a" * 40
    admitted = admit_claim("v0.21.5-rc", commit, on_main=lambda sha: sha == commit)

    assert admitted == {
        "claim_tag": "v0.21.5-rc", "tag": "v0.21.5",
        "version": "0.21.5", "commit": commit,
    }


def test_admission_refuses_a_claim_for_a_commit_off_main():
    from scripts.releases.stable import admit_claim

    with pytest.raises(ValueError, match="not on main"):
        admit_claim("v0.21.5-rc", "b" * 40, on_main=lambda _sha: False)


def test_admission_refuses_a_tag_that_is_not_a_claim():
    from scripts.releases.stable import admit_claim

    with pytest.raises(ValueError, match="claim"):
        admit_claim("v0.21.5", "a" * 40, on_main=lambda _sha: True)
