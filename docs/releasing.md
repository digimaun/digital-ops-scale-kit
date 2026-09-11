# Prepare and publish a release

Review the release tag and notes in a pull request. Merging the declaration
prepares one candidate from that exact commit. An authorized reviewer approves
tag creation and GitHub Release publication after the candidate is ready.

Site Ops and Scale Kit have independent version streams in the same repository.
A content release can reference an existing engine release without rebuilding
it or changing its version.

## Declare the release

Create a new directory for each release:

```text
releases/<name>/release.json
releases/<name>/notes.md
```

`release.json` contains the release intent. `notes.md` contains the Markdown
release notes. The tag is the version source for this declaration. The folder
name identifies the record, rather than providing another version setting.

Choose the declaration that matches the release. These examples illustrate
formats, not scheduled releases.

### Release Site Ops independently

```json
{
  "tag": "siteops/v1.1.0"
}
```

The version must exactly match `siteops.__version__` in the selected source
commit. The workflow builds the engine wheel with that source version, retains
its pinned runtime dependencies and notices, and qualifies the installation
bundle. Scale Kit content does not need a version change.

The current beta policy still keeps the source package at `1.0.0b1`.
Do not create another Site Ops beta tag without an approved version-policy
change.

### Release content against an existing engine

```json
{
  "tag": "v1.2.0",
  "siteops": {
    "release": "siteops/v1.1.0"
  }
}
```

This references a published Site Ops release in the same repository. It does
not build an engine bundle or depend on the current engine development version
on `main`. Confirm content compatibility with the referenced engine as part of
the release evidence.

The reference is an exact engine release, not a minimum-version range.
Stable content requires a stable referenced engine release.

Scale Kit content currently comes from the tagged repository. This workflow
does not produce a separately packaged workspace or claim that GitHub's
generated source archives are verified workspace packages.

### Include an identified engine build in a content preview

```json
{
  "tag": "v1.0.0b8",
  "siteops": {
    "build": true
  }
}
```

This is the combined preview path. The content tag advances independently,
while the source engine version follows its existing policy. The included
engine receives a distinguishable build version such as
`1.0.0b1+build.12345.1.gabcdef123456`.

It creates one content release with the Site Ops ZIP and detached attestation
attached. It does not create another Site Ops tag. This option requires a
prerelease content version. Stable content references a separately released
engine instead.

## Keep the declaration small

The workflow derives the release title and prerelease status from the tag.
`latest` defaults to false. A stable Scale Kit declaration may explicitly set
`"latest": true`. Site Ops releases use explicit tags rather than taking over
the repository's single Latest marker.

Do not commit source hashes, run IDs, artifact IDs, artifact digests, or a
`published` flag into the declaration. These are generated evidence or GitHub
state, not release intent. Keep published declarations as historical records
rather than maintaining a mutable `next release` file.

Notes are plain Markdown. Declarations cannot supply local hooks, commands,
arbitrary repository URLs, or custom note-file paths.

## Prepare and review the candidate

```text
Release declaration PR
          |
          v
Merge to main at commit A
          |
          v
Read declaration from A
Run CI for A
Build, attest, and qualify an engine bundle when requested
          |
          v
Review the candidate summary and applicable content evidence
          |
          v
Approve tag creation and publication
          |
          v
Tag A and publish the same qualified bytes
```

Use one declaration per release-preparation PR. Changes to its metadata or
notes start preparation. If several declarations change together, automatic
selection stops instead of choosing one silently.

For an explicitly selected new candidate, run the **Release** workflow on
`main`, supplying the committed declaration path as `intent` and the full
expected commit as `expected-source-sha`. If `main` moved, the request fails.
Use the original workflow run when reviewing an already prepared candidate.
The manual entry must exist on the default branch before GitHub accepts
dispatches.

The workflow reads metadata and notes from the selected Git commit. It does
not depend on a mutable checkout or select the newest successful build later.
Source and artifact identities remain fixed while approval is pending.
An unrelated advance of `main` does not change the candidate. Changing its
release declaration requires a fresh candidate and approval.

The approval summary shows the tag, source commit, version stream, engine
selection, bundle digest when applicable, tag action, and final release notes.
You may download the attested installation artifact for a hands-on trial.
Authenticate the installation ZIP before extracting or running its contents,
following [the installation guide](install-siteops.md).

CI and engine installation qualification are automated. For content releases,
the reviewer must also confirm the applicable content/AIO evidence and any
valid carry-forward from earlier qualification. The release workflow does not
deploy Azure resources or infer workload health from installation success.

## Approve publication

Configure the GitHub environment named `siteops-release` with required
reviewers and appropriate branch restrictions before preparing a release.
The workflow refuses to proceed without required reviewers. Choose self-review
and administrator-bypass settings according to repository policy.

After reviewing the generated summary, an authorized reviewer uses **Review
deployments**, selects `siteops-release`, then selects **Approve and deploy**.
This is GitHub's label for allowing the publishing job. It does not deploy
Azure infrastructure. If self-review is prevented, another reviewer must
approve the run.

After approval, the workflow:

1. Downloads and verifies the same declaration, notes, and qualified bundle.
2. Confirms the declaration has not changed and the referenced engine, if any,
   still has the same release identity and tag target.
3. Creates a missing tag at the approved commit, or reuses a tag already
   pointing there. It never moves a conflicting tag.
4. Creates the GitHub Release with the approved notes and declared assets.
5. Confirms the uploaded asset digests and verifies GitHub's release attestation
   when immutable releases are enabled.

The destination is the repository running the workflow. Official publication
occurs in `Azure/digital-ops-scale-kit`. A fork's workflow writes only to that
fork. Existing releases are never overwritten.

With installer assets, GitHub CLI creates a draft, uploads the files, and then
publishes. This temporary draft is not a separate human-review stage.
If immutability is enabled, GitHub locks the uploaded assets and tag when
publication completes. Titles and release notes remain editable through
GitHub's normal controls. The workflow does not enable immutability itself.

## Failures and rehearsals

A failed check stops publication. Missing artifacts, changed declarations,
conflicting tags, or an existing release require an explicit resolution rather
than a fallback to different source or replacement assets.

Tag creation and publication are separate GitHub operations. A failure can leave
the correct tag without a completed release. Inspect the result before retrying.
Rebuilding creates a new candidate with its own evidence and approval.

Use the CI workflow's `distribution-rehearsal` opt-in to exercise the shared
bundle build, signing, and installation path on a feature branch before merge.
Supply its exact commit in `distribution-source-sha`. The rehearsal creates
signed artifacts under the running repository's identity. It does not create
tags or publish releases.
