"""Fixtures for integration tests.

Integration tests deploy real manifests against Azure and assert outputs.
The test framework is site-agnostic: it deploys to whatever sites match
the manifest's selector (or a user-provided override), just like production.

Configuration is provided via:
  - Local: sites.local/ overlay files (hand-written YAML, one per site)
  - CI: SITE_OVERRIDES env var (JSON, auto-generates sites.local/ overlays)

If no sites match the selector, integration tests are skipped gracefully.
"""

import json
import os
from pathlib import Path

import pytest
import yaml

from siteops.orchestrator import Orchestrator

WORKSPACE_PATH = Path(__file__).parent.parent.parent / "workspaces" / "iot-operations"


def _generate_overlays_from_site_overrides() -> list[Path]:
    """Generate sites.local/ overlays from SITE_OVERRIDES env var.

    Uses the same dot-notation expansion as the CI deploy workflows.
    Returns list of generated overlay file paths.
    """
    raw = os.environ.get("SITE_OVERRIDES", "")
    if not raw:
        return []

    try:
        overrides = json.loads(raw)
    except json.JSONDecodeError:
        return []

    generated = []
    sites_local = WORKSPACE_PATH / "sites.local"
    sites_local.mkdir(parents=True, exist_ok=True)

    for site_name, site_data in overrides.items():
        overlay_path = sites_local / f"{site_name}.yaml"
        if overlay_path.exists():
            continue

        expanded: dict = {}
        for key, value in site_data.items():
            parts = key.split(".")
            target = expanded
            for part in parts[:-1]:
                target = target.setdefault(part, {})
            target[parts[-1]] = value

        overlay_path.write_text(yaml.dump(expanded, default_flow_style=False))
        generated.append(overlay_path)

    return generated


_generated_overlays: list[Path] = []


def pytest_collection_modifyitems(config, items):
    """Generate overlays from SITE_OVERRIDES and skip if no config available."""
    global _generated_overlays
    _generated_overlays = _generate_overlays_from_site_overrides()

    # Skip integration tests if no real Azure config is available:
    # no SITE_OVERRIDES env var and no sites.local/ directory
    sites_local = WORKSPACE_PATH / "sites.local"
    has_config = bool(_generated_overlays) or (
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
        for path in _generated_overlays:
            path.unlink(missing_ok=True)


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

