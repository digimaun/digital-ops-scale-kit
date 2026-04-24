"""Fixtures for integration tests.

Integration tests deploy real manifests against Azure and assert outputs.
The test framework is site-agnostic: it deploys to whatever sites match
the manifest's selector (or a user-provided override), just like production.

Configuration is provided via:
  - Local: sites.local/ overlay files (hand-written YAML, one per site)
  - CI integration suite: SITE_OVERRIDES env var (JSON → auto-generates sites.local/ overlays)
  - E2E suite: SITEOPS_EXTRA_SITES_DIRS env var (os.pathsep-joined dirs
    containing rendered site files; orthogonal to sites.local/)

Behavior when no site config is present:
  - Tests are skipped at collection time (`has_config` check).
Behavior when site config is present but the selector resolves to zero sites:
  - Tests ERROR at fixture time with a diagnostic message. A zero-site
    deployment is never a legitimate integration-test outcome; silent
    vacuous passes would mask real misconfigurations (wrong selector,
    broken inherits chain, mismatched labels) that were discovered
    previously in exactly this way.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from siteops.models import Manifest
from siteops.orchestrator import Orchestrator

WORKSPACE_PATH = Path(__file__).parent.parent.parent / "workspaces" / "iot-operations"
SCRIPT_PATH = Path(__file__).parent.parent.parent / "scripts" / "generate-site-overrides.py"

_EXTRA_SITES_DIRS_ENV = "SITEOPS_EXTRA_SITES_DIRS"


def _extra_sites_dirs() -> list[Path]:
    """Parse `SITEOPS_EXTRA_SITES_DIRS` into a list of paths (os.pathsep-delimited)."""
    raw = os.environ.get(_EXTRA_SITES_DIRS_ENV, "")
    return [Path(p) for p in raw.split(os.pathsep) if p.strip()]


def _extra_sites_have_yaml(dirs: list[Path]) -> bool:
    """Return True if any extra-sites dir contains at least one `*.yaml` or `*.yml` file."""
    return any(
        d.is_dir() and (any(d.glob("*.yaml")) or any(d.glob("*.yml")))
        for d in dirs
    )


def _generate_overlays_from_site_overrides() -> bool:
    """Generate sites.local/ overlays by calling the shared script.

    Returns True if overlays were generated.
    """
    raw = os.environ.get("SITE_OVERRIDES", "")
    if not raw.strip():
        return False

    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), str(WORKSPACE_PATH)],
        input=raw,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"generate-site-overrides.py failed: {result.stderr}", file=sys.stderr)
        return False

    return True


_pre_existing_overlays: set[str] = set()
_generated_overlays = False


def pytest_collection_modifyitems(config, items):
    """Generate overlays from SITE_OVERRIDES and skip if no config available."""
    global _generated_overlays, _pre_existing_overlays

    # Snapshot existing overlay files before generation
    sites_local = WORKSPACE_PATH / "sites.local"
    if sites_local.is_dir():
        _pre_existing_overlays = {f.name for f in sites_local.glob("*.yaml")}

    _generated_overlays = _generate_overlays_from_site_overrides()

    extra_dirs = _extra_sites_dirs()
    has_config = (
        _generated_overlays
        or (sites_local.is_dir() and any(sites_local.glob("*.yaml")))
        or _extra_sites_have_yaml(extra_dirs)
    )

    if not has_config:
        skip = pytest.mark.skip(
            reason="Integration tests require sites.local/ overlays, "
            "SITE_OVERRIDES, or SITEOPS_EXTRA_SITES_DIRS with site files"
        )
        for item in items:
            if "integration" in item.keywords:
                item.add_marker(skip)


def pytest_sessionfinish(session, exitstatus):
    """Clean up generated overlays unless skip-cleanup is set."""
    skip_cleanup = os.environ.get("INTEGRATION_SKIP_CLEANUP", "").lower() in ("true", "1", "yes")
    if _generated_overlays and not skip_cleanup:
        sites_local = WORKSPACE_PATH / "sites.local"
        if sites_local.is_dir():
            for f in sites_local.glob("*.yaml"):
                if f.name not in _pre_existing_overlays:
                    f.unlink(missing_ok=True)


@pytest.fixture(scope="session")
def workspace() -> Path:
    """Path to the IoT Operations workspace."""
    assert WORKSPACE_PATH.is_dir(), f"Workspace not found: {WORKSPACE_PATH}"
    return WORKSPACE_PATH


@pytest.fixture(scope="session")
def selector() -> str | None:
    """Site selector from INTEGRATION_SELECTOR env var, or None for manifest default."""
    return os.environ.get("INTEGRATION_SELECTOR") or None


@pytest.fixture(scope="session")
def orchestrator(workspace: Path) -> Orchestrator:
    """Orchestrator configured for the real workspace.

    `SITEOPS_EXTRA_SITES_DIRS` (os.pathsep-joined) is honored so the E2E
    workflow can inject a rendered site without touching `sites.local/`.
    """
    return Orchestrator(workspace, extra_trusted_sites_dirs=_extra_sites_dirs())


def _resolve_or_fail(
    orchestrator: Orchestrator, manifest_path: Path, selector: str | None
) -> tuple[Manifest, list]:
    """Resolve sites for a manifest, raising a diagnostic error on zero matches.

    The historical failure mode was a silent vacuous pass: selector resolved
    to an empty list, `deploy()` short-circuited with `sites={}`, and every
    test body's `for name in result["sites"]:` loop became a no-op. This
    helper makes that impossible at the fixture boundary.
    """
    manifest = Manifest.from_file(manifest_path)
    sites = orchestrator.resolve_sites(manifest, selector)
    if not sites:
        raise RuntimeError(
            f"Integration fixture resolved zero sites for manifest "
            f"'{manifest_path.name}' (selector={selector!r}, "
            f"manifest.siteSelector={manifest.site_selector!r}, "
            f"manifest.sites={manifest.sites!r}, "
            f"extra_trusted_sites_dirs={[str(p) for p in _extra_sites_dirs()]}). "
            f"A zero-site integration run indicates a configuration mismatch "
            f"(missing overlay, wrong selector, broken inherits chain, or "
            f"label mismatch) and is treated as a hard failure rather than "
            f"a silent pass."
        )
    return manifest, sites


@pytest.fixture(scope="session")
def aio_install_result(orchestrator: Orchestrator, selector: str | None) -> dict:
    """Deploy aio-install.yaml once, shared by all dependent tests."""
    manifest_path = WORKSPACE_PATH / "manifests" / "aio-install.yaml"
    manifest, sites = _resolve_or_fail(orchestrator, manifest_path, selector)
    result = orchestrator.deploy(
        manifest_path=manifest_path,
        manifest=manifest,
        sites=sites,
    )
    assert result["summary"]["failed"] == 0, (
        f"aio-install deployment failed: {result}"
    )
    return result


@pytest.fixture(scope="session")
def secretsync_result(
    orchestrator: Orchestrator, selector: str | None, aio_install_result: dict
) -> dict:
    """Deploy secretsync.yaml after AIO is installed."""
    manifest_path = WORKSPACE_PATH / "manifests" / "secretsync.yaml"
    manifest, sites = _resolve_or_fail(orchestrator, manifest_path, selector)
    return orchestrator.deploy(
        manifest_path=manifest_path,
        manifest=manifest,
        sites=sites,
    )


@pytest.fixture(scope="session")
def opc_ua_solution_result(
    orchestrator: Orchestrator, selector: str | None, aio_install_result: dict
) -> dict:
    """Deploy opc-ua-solution.yaml after AIO is installed."""
    manifest_path = WORKSPACE_PATH / "manifests" / "opc-ua-solution.yaml"
    manifest, sites = _resolve_or_fail(orchestrator, manifest_path, selector)
    return orchestrator.deploy(
        manifest_path=manifest_path,
        manifest=manifest,
        sites=sites,
    )

