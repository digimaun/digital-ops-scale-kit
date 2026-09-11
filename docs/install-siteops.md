# Install Site Ops from a release

Install an identified Site Ops build without cloning this repository. Release
bundles contain the engine wheel, pinned runtime dependency wheels, an installer,
and license notices. The same CLI works with local workspaces and automation.

Use this path with a release that provides `siteops-install.zip` and
`siteops-install.zip.attestation.jsonl`. A release without those assets uses the
[linked Site Ops release](releasing.md#release-content-against-an-existing-engine)
or the [source installation](../README.md#quick-start) path.

## Before you start

Use a private directory and run as your ordinary user, rather than as an
administrator or with `sudo`.

| Prerequisite | Requirement |
|---|---|
| Platform | Windows x64, or Linux x64 using glibc 2.17 or newer |
| Python | Standard 64-bit CPython 3.10 through 3.14, with working `venv` and `ensurepip` support |
| pipx | Version 1.17.2, or a compatible newer 1.x release, available as `pipx` |
| GitHub CLI | Version 2.95.0 or newer, available as `gh` |

Obtain these tools through your organization's managed software channel or
their official installation instructions:
[Python](https://www.python.org/downloads/),
[pipx](https://pipx.pypa.io/stable/installation/), and
[GitHub CLI](https://cli.github.com/).
They are maintained separately from the Site Ops bundle.
Some Linux distributions package Python's venv support separately.

The installer uses pipx's pip backend and locally supplied wheels. It does not
fetch Python, upgrade pipx's shared tooling, or build source distributions.
Prerequisite installation and signing-root refresh can require network access.
This is not a promise of a fully offline bootstrap.

Installing the CLI does not authenticate to Azure, acquire a workspace, or
deploy resources. Azure CLI, Bicep, and kubectl requirements depend on the
operations you later select.

## Select and authenticate a release

1. Open the [official releases](https://github.com/Azure/digital-ops-scale-kit/releases)
   and select an explicit release.
2. Confirm its tag and full source commit in the official repository. Keep
   that commit as the expected identity for verification.
3. Download both installation assets into your private directory. Keep the
   ZIP unopened until verification succeeds.

Versioned engine releases use `siteops/v...` tags. Content previews may include
an identified engine build. If a content release links to a separate Site Ops
release, follow that link and use the engine release's source commit for the
verification below, not the content release's commit.

The repository, signing workflow, and expected commit are trust decisions.
Take them from the official repository and this guidance, not from a downloaded
manifest or a command supplied inside the archive. A checksum beside a download
does not authenticate its publisher.

The commands below enforce official builds from `main`. A build from a fork
does not satisfy that policy. The signed provenance covers the entire ZIP,
including its installer and wheels.

### Windows PowerShell

Run from the directory containing the downloaded assets. Replace the source
commit placeholder with the full commit you selected.

```powershell
$sourceSha = "<full source commit from the selected official release>"

gh attestation verify .\siteops-install.zip `
  --bundle .\siteops-install.zip.attestation.jsonl `
  --repo Azure/digital-ops-scale-kit `
  --cert-identity "https://github.com/Azure/digital-ops-scale-kit/.github/workflows/_siteops-distribution.yaml@refs/heads/main" `
  --source-ref refs/heads/main `
  --source-digest $sourceSha `
  --signer-digest $sourceSha `
  --cert-oidc-issuer "https://token.actions.githubusercontent.com" `
  --predicate-type "https://slsa.dev/provenance/v1" `
  --deny-self-hosted-runners

if ($LASTEXITCODE -ne 0) {
    throw "Verification failed. Do not extract or install this bundle."
}

Expand-Archive -LiteralPath .\siteops-install.zip -DestinationPath .\siteops-bundle
python -B .\siteops-bundle\install.py
if ($LASTEXITCODE -ne 0) {
    throw "Installation did not complete. Follow the reported guidance."
}
```

Use a new extraction directory for each download. Follow the installer's PATH
guidance if the command directory is not already available in your terminal.

### Linux

Run from the directory containing the downloaded assets. Replace the source
commit placeholder. The `&&` chain stops before extraction or installation
when an earlier command fails.

```bash
source_sha="<full source commit from the selected official release>"
umask 077

gh attestation verify ./siteops-install.zip \
  --bundle ./siteops-install.zip.attestation.jsonl \
  --repo Azure/digital-ops-scale-kit \
  --cert-identity "https://github.com/Azure/digital-ops-scale-kit/.github/workflows/_siteops-distribution.yaml@refs/heads/main" \
  --source-ref refs/heads/main \
  --source-digest "$source_sha" \
  --signer-digest "$source_sha" \
  --cert-oidc-issuer "https://token.actions.githubusercontent.com" \
  --predicate-type "https://slsa.dev/provenance/v1" \
  --deny-self-hosted-runners &&
mkdir ./siteops-bundle &&
python3 -B -m zipfile -e ./siteops-install.zip ./siteops-bundle &&
python3 -B ./siteops-bundle/install.py
```

### What verification means

GitHub CLI validates the signing chain, the expected workflow and repository
identity, the source and signer commits, and the archive digest. Its
`--bundle` option reads the downloaded attestation rather than fetching it
through the GitHub attestation API. Default trusted-root refresh may still
contact the network.

The installer runs only after that verification. It checks the manifest and
payload hashes, retains the verified files, and installs from local wheel links
with SHA-256 constraints. It does not perform publisher-signature verification
itself.

Keep the downloaded and extracted files private between these steps. Signature
verification establishes origin and integrity, not that the selected source is
free of defects or that another process cannot modify your local files.
Fully offline verification requires independently provisioned trusted signing
roots. Never accept a replacement trust root merely because it accompanied
the download.

## Use the installed CLI

```text
siteops --version
siteops --help
```

The package and command report the exact artifact version. Versioned Site Ops
releases use their declared source package version. Identified builds add a
suffix, for example `1.0.0b1+build.12345.1.gabcdef123456`.
The engine version and Scale Kit content version remain separate identities.

Use `siteops -w <workspace>` with your local content. See
[site configuration](site-configuration.md) and
[manifest reference](manifest-reference.md) for the deployment model.

## Select another build, repair, or remove

Run these operations with the helper from an authenticated extracted bundle.
Linux uses `python3` in place of `python`.

| Task | Command |
|---|---|
| Install, or confirm the same build | `python -B <bundle>/install.py` |
| Upgrade or deliberately downgrade | `python -B <selected-bundle>/install.py --replace` |
| Repair the same selected build | `python -B <bundle>/install.py --reinstall` |
| Remove Site Ops | `python -B <bundle>/install.py --uninstall` |
| Choose private retained storage | Add `--store-dir <absolute-directory>` |

A different installed build requires `--replace`. Repair does not select a
different version. With no installation present, repair performs a fresh
installation. Removing an already absent installation succeeds.

Replacement uses the existing environment's Python interpreter. A fresh
installation uses `PIPX_DEFAULT_PYTHON` when configured, otherwise the base
interpreter running the helper. Changing the Python interpreter is a separate
pipx maintenance operation.

The helper checks for conflicting exposed commands before changing the
installation. It also refuses replacement or repair of pinned, injected,
separately locked, or non-isolated pipx environments. Resolve those explicit
operator customizations before using this installation path.

The installer supplies the expected `siteops` application to pipx so a failed
replacement can use pipx's native environment preservation. It does not
uninstall the previous build first. Forced process termination, power loss,
or storage failure is not a guaranteed rollback.

Ctrl-C requests a stop. An active pipx change finishes before the helper
returns, and the helper starts no further change. Mutating pipx commands do
not have an installer timeout. The final result reports interruption even
when the active change completed successfully.

## Retained files and private diagnostics

The installer retains the bundle because pipx records the wheel source and
installation arguments for later use.

| Platform | Default retained-data root |
|---|---|
| Windows | `%LOCALAPPDATA%\siteops\installations` |
| Linux | `$XDG_DATA_HOME/siteops/installations`, or `~/.local/share/siteops/installations` |

Each bundle has a content-addressed directory containing `payload` and
`wheel-links.html`. The reported data location identifies that directory.
Its `payload` includes a copy of `install.py` for later lifecycle operations.
The digest used as its directory name is an integrity key, not a separate
publisher signature.

Pipx continues to own its environment and command directories. Existing
`PIPX_HOME`, `PIPX_BIN_DIR`, `PIPX_MAN_DIR`, `PIPX_COMPLETION_DIR`, and
`PIPX_SHARED_LIBS` selections are honored and must be absolute paths outside
the extracted bundle. Installation storage must also stay outside the bundle.

POSIX storage is created with private permissions and requires trusted parent
directories. Windows uses inherited ACLs, so select a directory protected for
your account. The helper does not rewrite permissions on existing directories.

Private tool output is retained in `logs` under the data root. Removal keeps
retained bundles and diagnostics. Keep those bundles while an installation
uses them, and remove only identified obsolete data when reclaiming space.
The original extraction directory can be removed after successful installation.

## Automation output

Add `--output json` for one `SiteOpsInstallationResult` document on stdout.
Progress goes to stderr. Check the process exit code as well as the result.

| Exit code | Meaning |
|---|---|
| `0` | The requested operation completed, including an already satisfied state |
| `1` | Installation, tooling, or payload failure |
| `2` | Invalid command-line arguments |
| `130` | Stop requested, possibly after an active change completed |

The document identifies `apiVersion: siteops.install/v1`, package, status,
version, exit code, and interruption. Status is `installed`,
`already-installed`, `replaced`, `reinstalled`, `removed`, `not-installed`, or
`failed`. Command-line parser failures use argparse diagnostics rather than
this result document.

GitHub Actions and Azure Pipelines enable output redaction automatically.
`SITEOPS_REDACT_OUTPUT=1` enables it elsewhere. Redacted results omit local
paths and source identifiers while retaining fixed, actionable error messages.
Authorized private automation can use `SITEOPS_REDACT_OUTPUT=0`.
Do not publish the private pipx logs.

## Common problems

| Symptom | Action |
|---|---|
| Attestation verification fails | Stop before extraction. Confirm the selected release, source commit, both asset files, and trusted GitHub CLI installation. |
| No supported Python or wheel target | Use a standard supported CPython and platform combination. PyPy, free-threaded Python, musl, macOS, and ARM are outside this bundle matrix. |
| `pipx is required` | Install supported pipx through a trusted channel and make its command available in PATH. |
| A different build is installed | Use the authenticated selected bundle with `--replace`. |
| The exposed command conflicts | Inspect the existing command and resolve ownership before retrying. The helper leaves that command in place. |
| Retained content has changed | Investigate the existing entry. Re-extract the authenticated release and use a new private `--store-dir` with `--reinstall` rather than merging into damaged data. |
| A pipx step fails | Inspect its private log locally. Retain the reported exit status, even if an earlier part of the operation completed. |
