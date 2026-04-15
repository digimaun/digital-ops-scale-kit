"""Fixtures for integration tests.

Integration tests deploy real manifests against Azure and assert outputs.
The test framework is site-agnostic: it deploys to whatever sites match
the manifest's selector (or a user-provided override), just like production.

Configuration is provided via:
  - Local: sites.local/ overlay files (hand-written YAML, one per site)
  - CI: SITE_OVERRIDES env var (JSON, auto-generates sites.local/ overlays)

If no sites match the selector, integration tests are skipped gracefully.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from siteops.orchestrator import Orchestrator

WORKSPACE_PATH = Path(__file__).parent.parent.parent / "workspaces" / "iot-operations"
SCRIPT_PATH = Path(__file__).parent.parent.parent / "scripts" / "generate-site-overrides.py"


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

    has_config = _generated_overlays or (
        sites_local.is_dir() and any(sites_local.glob("*.yaml"))
    )

    if not has_config:
        skip = pytest.mark.skip(
            reason="Integration tests require sites.local/ overlays "
            "or SITE_OVERRIDES env var"
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
    """Orchestrator configured for the real workspace."""
    return Orchestrator(workspace)


@pytest.fixture(scope="session")
def aio_install_result(orchestrator: Orchestrator, selector: str | None) -> dict:
    """Deploy aio-install.yaml once, shared by all dependent tests."""
    result = orchestrator.deploy(
        manifest_path=WORKSPACE_PATH / "manifests" / "aio-install.yaml",
        selector=selector,
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
    return orchestrator.deploy(
        manifest_path=WORKSPACE_PATH / "manifests" / "secretsync.yaml",
        selector=selector,
    )


@pytest.fixture(scope="session")
def opc_ua_solution_result(
    orchestrator: Orchestrator, selector: str | None, aio_install_result: dict
) -> dict:
    """Deploy opc-ua-solution.yaml after AIO is installed."""
    return orchestrator.deploy(
        manifest_path=WORKSPACE_PATH / "manifests" / "opc-ua-solution.yaml",
        selector=selector,
    )

