# Stable release admission and promotion

`Stable Release` is the release gate. A successful builder alone is not a
stable release. Stable releases use claim tags as locks and final tags as green
receipts; neither tag is a workflow trigger. Canary builds retain their separate
push-driven workflow.

The committed project version is always `0.0.0`. Release jobs derive the payload
version from the admitted ref and stamp isolated build trees. Do not bump version
files on `main`.

## Order

1. Refresh `origin/main` and remote `v*-rc` claims. Derive the next SemVer from
   the published release family seeded at `0.21.4` and every spent claim.
2. Push an annotated `vMAJOR.MINOR.PATCH-rc` claim atomically, create one
   non-prerelease GitHub draft for it, and dispatch `Stable Release` on that exact
   claim ref. The claim message binds its commit, autopublish policy, and one
   monotonically allocated epoch. That epoch is the release date and native
   packaging clock for every matrix leg and retry.
3. Admit the exact remote annotated tag-object SHA, peeled commit, `GITHUB_REF`,
   `GITHUB_SHA`, checked-out `HEAD`, and ancestry on `origin/main`.
4. Run the whole source, Docker, Nix, PM bundle, install/update, Termux, Windows,
   signed-package, and native-upgrade acceptance graph.
5. Publish immutable versioned Docker and R2 artifacts from the tested bytes.
   Do not move `stable` or `latest` aliases yet and do not rebuild for publication.
6. Create the annotated final `vMAJOR.MINOR.PATCH` receipt. It binds the original
   claim object, commit, candidate-manifest SHA256, Docker manifest digest, and
   autopublish policy. Retarget the existing draft to this final tag.
7. The stable publication controller resolves releases oldest first. It publishes
   the GitHub draft, verifies and promotes the receipt-bound Docker digest, then
   advances App Installer, macOS, APT, downloads-page, and protected R2 heads.

A sole green draft waits unless its claim selected autopublish. A later green
claim flushes all contiguous earlier green drafts in order. An older running claim
blocks newer publication. Burned claims are skipped but their version numbers are
never reused. Failed, cancelled, missing, and unexpectedly skipped requirements
remain red.

Docker Hub, R2, APT, GitHub, and the Store do not support one cross-service
transaction. The controller is therefore idempotent: after a partial failure,
run it again and let every mutation verify its current state before continuing.
The final tag is the custody receipt. Never rebuild or replace accepted bytes to
repair a pointer.

Promotion replaces the stable downloads page at `releases/stable/index.html` on
the R2 public origin. Canary tag builds own `releases/canary/index.html`, and
commit builds own `releases/commit/<sha>/index.html`. Pages list only staged,
receipt-backed objects; an older run cannot regress a protected channel.

## Run, publish, or abandon a stable release

Start from an exact commit already on remote `main`:

```sh
python scripts/release.py release --commit "$(git rev-parse origin/main)" --bump patch --remote origin
```

`--bump` defaults to `patch`; pass `minor` or `major` only when that change is
intentional. The selected commit must descend from or equal the highest claim's
source. Equality lets a burned release be superseded without an unrelated code
change; an ancestor or unrelated sibling is refused.

Add `--autopublish` to publish immediately when the claim becomes the oldest
green release. Without it, the release stays a draft until an explicit publish
or a later green claim forces ordered resolution.

The claim push is the atomic version lock. Two callers may derive the same next
version, but only one push wins; the loser reports the winning tagger, time, and
commit. A rejected or abandoned claim remains spent. To publish or abandon:

```sh
python scripts/release.py publish --version 0.21.5 --remote origin
python scripts/release.py abandon --version 0.21.5 --remote origin
```

`publish` performs a synchronous supersession preflight, then dispatches the same
ordered controller used by automatic recovery. It refuses a known burned version
below a newer published release. `abandon` deletes only the draft; it deliberately
keeps the `-rc` and any final receipt so derivation cannot reuse that version.
The sequencer derives that missing-draft state as burned rather than recording a
separate abandonment flag.

Do not manually dispatch `Stable Release` from a final tag. Recovery keeps the
original claim ref, object SHA, commit, draft database ID, and autopublish policy.

## Failure and recovery

A failed stable run becomes retry-eligible after 15 minutes, then reruns only
failed jobs in the same GitHub Actions run. The quarter-hour reconciler applies
only the oldest unresolved eligible retry, because GitHub keeps only one pending
run in the shared stable-release concurrency group. It does not occupy a runner
during the backoff. At most two retries are admitted (run attempts 2 and 3).
After attempt 3 fails, the claim is burned and the sequencer may resolve later
claims. The same controller recovers a lost retry request or a crash between any
publication mutations.

A newly pushed claim with no observed workflow run remains unresolved for one
hour. This grace window covers the non-atomic draft and dispatch steps. After the
hour, a still-unstarted claim is derived as burned. No claim is burned during the
normal creation window.

Desktop and Termux handoffs live in the immutable R2 tag archive. Each producer
writes a `handoff-<target>.json` receipt containing the tag, commit, paths, sizes,
and SHA256 digests. Candidate assembly emits one pinned
`release-candidates.json`; consumers verify its digest and do not re-upload the
packages. Docker publication pushes one immutable versioned manifest and records
its registry digest in the final tag. Delayed publication promotes that digest
registry-side, so it does not depend on expiring Actions artifacts.

Windows, macOS, and Termux candidate jobs stage packages and metadata under
`releases/tag/<tag>/` before acceptance. No stable feed is written at this stage.
Immutable uploads accept an existing object only when its bytes match. If an
archive object, final-tag digest, versioned Docker tag, or read-back differs, stop
recovery rather than replacing the accepted candidate.

The candidate manifest binds the admitted claim epoch. Admission recomputes the
stable Windows quad from that epoch and rejects a merely well-formed but incorrect
MSIX, executable VERSIONINFO, or App Installer version. Native admission also
reads the Electron artifact filename and macOS plist from the built packages. The
acceptance graph stamps an isolated bootstrap-installer tree, asks Cargo to read
the resulting Tauri package version, builds and inspects Python wheel/sdist
metadata and filenames, and checks the Nix and Docker runtime identities. Any
consumer-facing `0.0.0` or mismatched version fails the gate.

The annotated final tag binds the GitHub release database ID admitted with the
claim. Deleting that release burns the transaction: a replacement draft with the
same tag name cannot be retargeted or published by reconciliation.

The desktop workflow's optional `termux_upgrade_from_tag` input names an exact
published release with a Termux R2 handoff. Explicit non-publishing desktop builds
retain no downloadable job artifacts.

## Tag namespaces and receipts

Before relying on claim tags as locks, apply a repository ruleset for
`refs/tags/v*` that restricts creation, update, and deletion to organization
administrators and the release integration. Claim and final tags are immutable.
Channel and commit builds write annotated post-build receipts such as
`v0.0.7+channel.<YYYYMMDDTHHMMSSZ>.<run-id>` and
`v0.0.0+commit.<YYYYMMDDTHHMMSSZ>.<run-id>` only after publication succeeds.
Receipt tags do not create GitHub releases and do not trigger workflows.

Canary source identity is only
`v<stable>+canary.<YYYYMMDDTHHMMSSZ>`. Build metadata intentionally makes it
SemVer-equal to its stable core; the protected channel record and embedded UTC
timestamp decide progression. Desktop clients treat that validated channel
sequence as the update authority rather than asking SemVer to order build
metadata. Every `main` push queues a canary run with
`cancel-in-progress: false`. Historical `-canary.` identities are unsupported.
If a process stops after pushing the canary tag, rerunning the command verifies
the exact remote tag object, repairs the missing draft, and redispatches until the
protected canary head receipts that tag.
The protected publication controller verifies that exact annotated tag object
before it flips the GitHub prerelease from draft to public, reads both states back,
and only then advances the R2 head.

## Canary and one-off desktop identities

`release.py --canary` builds the separate canary application. Its package
identity and CLI command (`hermes-canary`) differ from stable; the existing
canary feed updates that application only. One-off builds use
`release.py --build-commit REV --remote REMOTE` (add `--publish` to dispatch).
Their application identity and CLI command (`hermes-<7-character-sha>`) include
the pinned commit. Two different commit builds do not replace each other.

Branding is selected from those build inputs, not from runtime settings:
canary uses yellow/dark-yellow icons; one-off builds use red icons bearing
the short SHA. All desktop icon formats derive from the same artwork.

One-off stamps use `source: commit-build`. No app update feed or App Installer
subscription is published for them, and both the GUI and bundled CLI refuse
update requests. They direct the recipient to ask the developer for a new
build. Source checkout channels are separate: `hermes update --set-channel`
remains available there and selects the published release's source commit.

`--build-commit` prints its deterministic downloads-page URL before dispatch,
including in dry runs:
`https://hermes-assets.nousresearch.com/releases/commit/<full-sha>/index.html`.
`CLOUDFLARE_R2_PUBLIC_URL` overrides the public origin. After admission, the
commit summary runs even when a build or assembly job fails; it lists only
receipt-backed existing downloads and marks missing binaries as not built.
Missing binaries link to the workflow run under **View build run**, not to
nonexistent downloads. Disabled platforms have no download or failure link.
Page publication still requires working R2 access. The commit links to its source
on GitHub; tag and channel pages link to the corresponding GitHub release tag.
Commit pages also list explicit non-secret `--bundle-env` defaults and
`--bundle-unset` clears passed to the desktop bundles, not the CI environment.
Values are shown as JSON strings (including `""` for an empty value); clears are
labeled **Unset**. The section is omitted when no overrides were supplied.

Tagged builds also publish a per-tag diagnostic page at
`releases/tag/<tag>/index.html` after build or feed failures, including when no
artifacts were uploaded. An incomplete build does not advance the channel page
or pass the release-success gate.

Store submission retains its fixed official stable identity. Nonstable
packages must not be submitted under that identity.

## Dynamic channels in R2

Channel names are R2 objects, not a repository registry. A preview channel owns
one native application identity across exact-commit builds. The immutable build
request records its source commit, bundle defaults, channel sequence and package
versions separately. The existing native build and smoke jobs must all pass
before the channel head advances.

Preview a custom build, then explicitly dispatch it. Repository identity does
not select behavior: the same direct dispatch runs from any GitHub remote, but
`--channel` performs no R2 access locally — it resolves the exact pushed commit
and dispatches the default-branch workflow, whose privileged allocation step
creates the channel and mints the immutable build request in CI. The local
command needs only a `gh` token with write, maintain or admin permission on the
selected repository; no R2 credentials are required.

```sh
python scripts/release.py --channel pm-preview --build-commit my-branch --remote origin
python scripts/release.py --channel pm-preview --build-commit my-branch --remote origin --publish
python scripts/release.py --channels --remote origin
```

Disposable R2 scoping is opt-in, for test runs only. Dispatch the desktop
workflow with `disposable_channel` and `build_commit` to allocate a namespace
under `ci-disposable/<repository-id>/<run-id>/`, then use the exact scoped build
command from its summary. For local administration of that allocation, carry
its `R2_DISPOSABLE_RUN` and `GITHUB_REPOSITORY_ID` in the command environment,
with the configured public URL still at the unscoped root. These are test-run
inputs, not persistent application settings; the repository ID is checked
against the selected remote before credentials are read, so a scoped namespace
still belongs to exactly one repository.

The first publishing invocation creates the channel (during CI allocation), and
later invocations retain its identity. Requests and artifacts live under
`releases/channel-builds/BUILD_ID/`; the mutable pointer is
`releases/channels/NAME.json`. A failed build leaves its previous head intact.
Conditional writes reject stale publication and permanently retired channels.
Retrying re-dispatches `--channel NAME --build-commit SHA --publish`, which
allocates a fresh sequence slot in CI; there is no separate resume command.

Retirement pins the current official stable build as the first receiver:

```text
python scripts/release.py --retire-channel pm-preview --to stable --minimum-version VERSION --remote origin
```

Add `--publish` only after reviewing the dry run and the exact native acceptance
evidence. This does not rebuild stable under the preview identity or silently
uninstall clients. Protocol-aware clients offer a consented cross-application
handoff; the destination must confirm readiness before preview removal. Keep
the retirement object and pinned artifacts available for offline clients.
Existing one-off builds have no retirement reader and require replacement.

The destination manifest must declare receiver support read from its packaged
stamp. The existing protected release workflow owns acceptance; there is no
separate public certification document or dependency on expiring Actions artifacts.
Native signature and identity checks, recipient consent, preserved-state preflight,
and destination readiness remain mandatory. An offline preview reaches its pinned
first receiver even after stable advances; that app then updates through stable.

Before shipping R2-only source readers, seed the existing `main` source-branch
record and published stable/canary records through the explicit protected
bootstrap operation. Review actual accepted manifests; do not invent a native
manifest for `main`. Production seeding, CDN cache/CAS verification and native
signed-package qualification are release operations, not implied by a passing
local helper suite. Never store the bootstrap output as a repo channel list.

## Signed-package baseline

The last successful stable release records
`releases/stable/release-candidates.json` on the configured R2 public origin.
It identifies actual Windows universal MSIX bundles, macOS ZIPs and package
provenance. The next run combines those records with its candidate manifest
and uses the existing native bundled-update drivers.

For an existing stable release that predates this metadata, supply
`baseline-manifest` as an HTTPS URL on the configured R2 public origin to a
current schema-2 manifest of its actual published packages and successful native
smoke results. Schema-1 candidates are rejected; there is no legacy admission
path. Manifest redirects must stay on the same origin. The baseline tag must be
a published stable release, package identities must agree, and versions must
increase. Stable Windows
packages use electron-builder's `storePackageVersionAt` policy over the admitted
claim tag object's immutable tagger timestamp (`year.hourOfYear.secondOfHour.0`),
independently of the SemVer payload tag.
Missing baseline
artifacts are a blocker, not permission to fabricate or skip acceptance.
See [the bundled update contract](https://github.com/NousResearch/hermes-agent/blob/main/tests/install/BUNDLED_UPDATES.md).

## Explicit exclusions and policy

- Desktop Playwright E2E (`e2e-desktop.yml`) is deferred at the owner's request
  because it is flaky. It is reported as deferred, not passed. Stabilize it
  and prove repeatable CI runs before adding it to this gate.
- Install/update E2E and native signed-package acceptance are **not** deferred.
- PR-only history, label and diff review checks do not apply to a stable tag.
  All applicable source CI jobs still run, regardless of changed paths.
- OSV vulnerability findings retain their existing advisory policy. Required
  scanner execution failures are failures, not advisory findings.
- Disabled Linux desktop packaging is not claimed as shipped. Native Linux
  PM bundles, Docker, Nix and install/update checks remain required.
- Housekeeping, autofix, comment and skills-index/deploy workflows are not
  release acceptance suites.

## Implementation ownership

Actions owns job ordering, runner selection, permissions and environments.
Python owns shared release admission, manifests, artifact hashes, publication
and channel promotion under `scripts/releases/` and `scripts/bundles/`.
Electron-builder configuration/hooks and native Windows/macOS adapters remain
in JavaScript or PowerShell. These adapters consume release facts rather than
reimplementing the release gate. Gate jobs use only Python's standard library;
they do not install the application or the JS workspace to report a verdict.

`scripts.bundles.release_artifacts` owns App Installer XML and feed publication.
Its serializer takes explicit package identity, publisher, version, subscription
URI and artifact URI. Stable promotion uses the accepted candidate metadata;
canary publication verifies the native bundle manifest against the adapter's
expected identity before uploading the bundle, then the descriptor. Native SDK
bundling/signing stays in `stage-msixbundle.mjs`. Store and commit builds stop
before that feed handoff; `stable-store` submits the verified candidate without
rebuilding it. Native acceptance uses the same Python serializer, including its
12-hour on-launch check policy.

Signing and publication credentials stay in their protected job environments.
The source CI call does not inherit deployment secrets. Configure the existing
release-signing and container-publish environments before running this pipeline.
