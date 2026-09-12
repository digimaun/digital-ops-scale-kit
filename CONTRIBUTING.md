# Contributing

This project welcomes contributions and suggestions.

## Development Setup

```bash
# Clone the repository
git clone https://github.com/Azure/digital-ops-scale-kit.git
cd digital-ops-scale-kit

# Install with dev dependencies
pip install -e ".[dev]"

# Run tests
pytest -m "not integration"

# Run tests with coverage
pytest -m "not integration" --cov=siteops --cov-report=term-missing
```

## Code Style

- Type hints required for all functions
- Docstrings for public methods
- Follow existing patterns in the codebase

## Testing

- Add tests for new functionality
- Inject command runners for planning and executor tests. Guard
  `subprocess.Popen` when a real process would violate the test boundary.
- Use fixtures from `conftest.py` for workspace setup
- Install the development dependencies before running packaging tests. They
  build a wheel in a temporary directory with package-index access disabled.
- Native installation tests use pipx in isolated temporary directories. On
  Windows, select a short owned `pytest --basetemp` path so generated executable
  paths remain within the platform limit. For offline tests, set
  `SITEOPS_TEST_BACKEND_WHEELHOUSE` to a directory holding the pip wheel pinned
  in `scripts/siteops-build-requirements.txt`.

## Pull Request Process

1. Run `pytest -m "not integration"` and `ruff check .`
2. Update documentation if adding new features
3. Follow the existing code style

## Versioning

This repository uses two independent version streams with [semantic versioning](https://semver.org/):

### Scale Kit (content)

Git tags: `v1.0.0b1`, `v1.1.0`, `v2.0.0`

Covers workspace content: Bicep templates, manifests, parameter files, site examples, GitHub
workflows, and documentation. Unscoped `v*` tags are the primary release. GitHub Releases attach
to these tags and note the minimum required siteops version.

### Site Ops (tool)

Git tags: `siteops/v1.0.0b1`, `siteops/v1.1.0`

Covers the `siteops/` Python package: CLI, orchestrator, executor, models. The `siteops/v*` tag
stays in sync with the version in `siteops/__init__.py` (read dynamically by pyproject.toml).

### Guidelines

- The scale kit version cannot be more stable than siteops. If siteops is beta, the scale kit
  is beta.
- Keep the checked-in `siteops.__version__` at `1.0.0b1` throughout the
  `v1.0.0b*` Scale Kit content beta series. Generated experimental artifacts
  may add a PEP 440 local version identifying the build, attempt, and source
  commit. The installed package and CLI report that full version.
  Do not create another `siteops/v*` beta tag unless the release policy changes.
- When content requires a new Site Ops version, release that engine version
  first and reference it from the content declaration. Independent releases
  need not share a commit.
- Content-only changes (new templates, manifest updates, doc fixes) bump only the `v*` tag.
- Tool-only changes (CLI features, orchestrator fixes) bump only the `siteops/v*` tag.
- Use conventional commits to distinguish change types:
  - `feat(workspace):` for new content
  - `feat(siteops):` for new tool features
  - `fix(siteops):` for tool bugfixes
  - `docs:` for documentation

### Example version streams

These examples illustrate the version policy, not a release schedule:

```text
v1.0.0b1 + siteops/v1.0.0b1    first public beta
v1.0.0b2                      another content beta, retaining siteops 1.0.0b1
v1.0.0 + siteops/v1.0.0        stable release
v1.1.0                        content-only feature
siteops/v1.0.1                tool-only fix
v1.2.0 + siteops/v1.1.0        content requiring a newer tool version
```

## Site Ops release artifacts

Use the [installation guide](docs/install-siteops.md) to install a release wheel
or verified bundle through pipx, and the [release guide](docs/releasing.md) to
prepare and publish them.
Release declarations and notes are reviewed in Git. The release workflow
prepares an exact candidate, shows an approval preview, and creates its tag and
GitHub Release only after approval.

Site Ops releases use the source package version. Content releases reference
an already published engine or explicitly include an identified engine build
during preview. Neither stream forces a version increment in the other.

There are two operator entry points: **CI** for checks and previews, and
**Release (approval required)** for real publication. They share the read-only
candidate workflow and `_siteops-distribution.yaml` build/signing machinery.
CI offers `run-mode: ci-only`, `installer-check`, or `release-preview`. The
latter two use `expected-source-sha`. The release preview also accepts
`release-file`, defaulting to a committed example. No CI mode publishes.

### Produce local artifacts

Use a clean checkout and an isolated Python 3.11 or newer build environment. Install
`scripts/siteops-build-requirements.txt` with pip's `--require-hashes` and
`--only-binary=:all:` options through your approved package feed.

```text
python scripts/build-siteops-bundle.py
  --repository <owner/repository>
  --source-ref <full Git ref>
  --expected-source-sha <full source commit>
  --build-number <positive integer>
  --build-attempt <positive integer>
  --output <absolute output directory>/siteops-install.zip
  --download-dependencies
```

Join the lines for your shell. `--download-dependencies` explicitly allows
hash-pinned runtime wheel downloads from the configured feed. Alternatively,
use `--wheelhouse <directory>` with the complete locked wheel set. Exactly
one of these options is required. The output directory receives
`siteops-install.zip` and the standalone Site Ops wheel. Both contain identical
application wheel bytes. Neither existing artifact is overwritten.
`--version-mode build` is the default and adds the build identity suffix.
`--version-mode source` retains the source package version for an independently
versioned engine release.

The producer exports tracked source and derives the artifact version and
dependency pins only in staging. The ZIP contains that wheel, runtime wheels,
their relative paths and hashes in `pylock.toml`, the bundle inventory, and
license notices. Native pipx manages installation and removal. The lock reader
requires the supported pipx backend described in the installation guide.
Local generation creates neither GitHub attestations nor an official release.

When changing runtime dependencies, update the runtime lock, target wheel
coverage, and notices together. Every declared target must have one compatible
wheel per locked package, and each runtime wheel's dependencies must be
satisfied by unconditional entries in that lock. Conditional runtime
dependencies, extras, and direct URLs require a separately defined policy.
Build tooling is pinned separately and is not redistributed in the archive.

## Microsoft Open Source

Most contributions require you to agree to a Contributor License Agreement (CLA) declaring that you have the right to, and actually do, grant us the rights to use your contribution. For details, visit <https://cla.opensource.microsoft.com>.

This project has adopted the [Microsoft Open Source Code of Conduct](https://opensource.microsoft.com/codeofconduct/). For more information see the [Code of Conduct FAQ](https://opensource.microsoft.com/codeofconduct/faq/).
