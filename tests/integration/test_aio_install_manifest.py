"""Integration tests for the aio-install.yaml manifest."""

import pytest

from tests.integration.helpers.assertions import (
    assert_output_exists,
    assert_step_skipped,
    assert_step_succeeded,
    find_step,
)
from tests.integration.conftest import WORKSPACE_PATH

pytestmark = [pytest.mark.integration]


class TestAioInstallDeployment:
    """Validate that aio-install.yaml deploys successfully."""

    def test_no_failures(self, aio_install_result):
        assert aio_install_result["summary"]["failed"] == 0

    def test_all_sites_succeeded(self, aio_install_result):
        for name in aio_install_result["sites"]:
            site = aio_install_result["sites"][name]
            assert site["status"] == "success", f"Site '{name}' failed: {site.get('error')}"

    def test_schema_registry_outputs(self, aio_install_result):
        for name in aio_install_result["sites"]:
            step = assert_step_succeeded(aio_install_result, name, "schema-registry")
            assert_output_exists(step, "schemaRegistry")

    def test_adr_ns_outputs(self, aio_install_result):
        for name in aio_install_result["sites"]:
            step = assert_step_succeeded(aio_install_result, name, "adr-ns")
            assert_output_exists(step, "adrNamespace")

    def test_aio_enablement_outputs(self, aio_install_result):
        for name in aio_install_result["sites"]:
            step = assert_step_succeeded(aio_install_result, name, "aio-enablement")
            assert_output_exists(step, "clExtensionIds")

    def test_aio_instance_outputs(self, aio_install_result):
        for name in aio_install_result["sites"]:
            step = assert_step_succeeded(aio_install_result, name, "aio-instance")
            assert_output_exists(step, "aio")
            assert_output_exists(step, "customLocation")
            assert_output_exists(step, "aioExtension")

    def test_schema_registry_role_succeeds(self, aio_install_result):
        for name in aio_install_result["sites"]:
            assert_step_succeeded(aio_install_result, name, "schema-registry-role")


class TestAioInstallConditionalSteps:
    """Validate that conditional steps are gated correctly."""

    def test_global_edge_site_skipped_for_rg_sites(self, aio_install_result):
        """RG-level sites should skip the subscription-scoped edge site step."""
        for name in aio_install_result["sites"]:
            step = find_step(aio_install_result, name, "global-edge-site")
            assert step["status"] == "skipped", (
                f"Site '{name}': global-edge-site should be skipped for RG-level sites"
            )

    def test_secretsync_steps_skipped_when_disabled(
        self, aio_install_result, orchestrator
    ):
        """Sites with deployOptions.enableSecretSync=false should skip both
        secretsync steps embedded in aio-install.yaml (a regression guard for
        the E2E site template and anyone reusing the same deployOptions)."""
        for name in aio_install_result["sites"]:
            site = orchestrator.load_site(name)
            enabled = site.properties.get("deployOptions", {}).get("enableSecretSync", True)
            if enabled:
                continue
            assert_step_skipped(aio_install_result, name, "resolve-aio")
            assert_step_skipped(aio_install_result, name, "secretsync")

    def test_solution_step_skipped_when_disabled(
        self, aio_install_result, orchestrator
    ):
        """Sites with deployOptions.includeSolution=false should skip the
        opc-ua-solution step embedded in aio-install.yaml."""
        for name in aio_install_result["sites"]:
            site = orchestrator.load_site(name)
            included = site.properties.get("deployOptions", {}).get("includeSolution", True)
            if included:
                continue
            assert_step_skipped(aio_install_result, name, "opc-ua-solution")


class TestAioInstallVersioning:
    """Validate that the AIO extension Azure actually deployed matches the
    versioned-templates contract (requested aioVersion selects a template dir
    that pins the extension version)."""

    def test_aio_extension_version_reported(self, aio_install_result):
        """The bicep output `aioExtension.version` reflects
        `Microsoft.KubernetesConfiguration/extensions/.../properties/version`
        from Azure. Assert it is present and non-empty for every site; this
        is the primary regression guard for versioned-templates wiring."""
        for name in aio_install_result["sites"]:
            step = assert_step_succeeded(aio_install_result, name, "aio-instance")
            aio_extension = assert_output_exists(step, "aioExtension")
            assert isinstance(aio_extension, dict), (
                f"Site '{name}': aioExtension output is not an object: {aio_extension!r}"
            )
            version = aio_extension.get("version")
            assert version, (
                f"Site '{name}': aioExtension.version is missing or empty "
                f"(keys: {sorted(aio_extension.keys())})"
            )


class TestAioInstallIdempotency:
    """Validate that re-deploying produces the same results."""

    def test_redeploy_succeeds(self, orchestrator, selector, aio_install_result):
        result2 = orchestrator.deploy(
            manifest_path=WORKSPACE_PATH / "manifests" / "aio-install.yaml",
            selector=selector,
        )
        assert result2["summary"]["failed"] == 0
