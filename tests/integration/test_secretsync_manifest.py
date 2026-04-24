"""Integration tests for the secretsync.yaml manifest."""

import pytest

from tests.integration.helpers.assertions import (
    assert_output_exists,
    assert_output_starts_with,
    assert_step_succeeded,
    find_step,
)
from tests.integration.conftest import WORKSPACE_PATH

pytestmark = [pytest.mark.integration]


class TestSecretSyncDeployment:
    """Validate that secretsync.yaml deploys successfully."""

    def test_no_failures(self, secretsync_result):
        assert secretsync_result["summary"]["failed"] == 0

    def test_all_sites_succeeded(self, secretsync_result):
        for name in secretsync_result["sites"]:
            site = secretsync_result["sites"][name]
            assert site["status"] == "success", f"Site '{name}' failed: {site.get('error')}"
            assert site["steps_completed"] == 2


class TestSecretSyncResolveAio:
    """Validate resolve-aio step outputs across all sites."""

    def test_resolve_aio_succeeds(self, secretsync_result):
        for name in secretsync_result["sites"]:
            assert_step_succeeded(secretsync_result, name, "resolve-aio")

    def test_infrastructure_outputs(self, secretsync_result):
        for name in secretsync_result["sites"]:
            step = assert_step_succeeded(secretsync_result, name, "resolve-aio")
            assert_output_exists(step, "customLocationName")
            assert_output_exists(step, "customLocationNamespace")
            assert_output_exists(step, "connectedClusterName")
            assert_output_starts_with(step, "customLocationId", "/subscriptions/")

    def test_oidc_issuer_url(self, secretsync_result):
        for name in secretsync_result["sites"]:
            step = assert_step_succeeded(secretsync_result, name, "resolve-aio")
            assert_output_starts_with(step, "oidcIssuerUrl", "https://")

    def test_instance_properties_forwarded(self, secretsync_result):
        for name in secretsync_result["sites"]:
            step = assert_step_succeeded(secretsync_result, name, "resolve-aio")
            assert_output_exists(step, "instanceLocation")
            assert_output_starts_with(step, "schemaRegistryResourceId", "/subscriptions/")
            assert_output_exists(step, "identityType")


class TestSecretSyncEnablement:
    """Validate secretsync step outputs across all sites."""

    def test_secretsync_succeeds(self, secretsync_result):
        for name in secretsync_result["sites"]:
            assert_step_succeeded(secretsync_result, name, "secretsync")

    def test_spc_created(self, secretsync_result):
        for name in secretsync_result["sites"]:
            step = assert_step_succeeded(secretsync_result, name, "secretsync")
            assert_output_starts_with(step, "spcResourceId", "/subscriptions/")
            assert_output_exists(step, "spcResourceName")

    def test_managed_identity_created(self, secretsync_result):
        for name in secretsync_result["sites"]:
            step = assert_step_succeeded(secretsync_result, name, "secretsync")
            assert_output_exists(step, "managedIdentityPrincipalId")
            assert_output_exists(step, "managedIdentityClientId")
            assert_output_starts_with(step, "managedIdentityResourceId", "/subscriptions/")

    def test_key_vault_created(self, secretsync_result):
        for name in secretsync_result["sites"]:
            step = assert_step_succeeded(secretsync_result, name, "secretsync")
            assert_output_exists(step, "keyVaultName")
            assert_output_starts_with(step, "keyVaultResourceId", "/subscriptions/")

    def test_federated_credential_created(self, secretsync_result):
        for name in secretsync_result["sites"]:
            step = assert_step_succeeded(secretsync_result, name, "secretsync")
            assert_output_exists(step, "federatedCredentialName")


class TestSecretSyncIdempotency:
    """Validate that re-deploying produces consistent results."""

    def test_redeploy_succeeds_with_same_outputs(self, orchestrator, selector, secretsync_result):
        """Every resource secretsync creates is expected to be idempotent. A
        regression where the MI, KV, or SPC silently gets recreated would
        break workload-identity federation and any dependent site."""
        result2 = orchestrator.deploy(
            manifest_path=WORKSPACE_PATH / "manifests" / "secretsync.yaml",
            selector=selector,
        )
        assert result2["summary"]["failed"] == 0

        stable_outputs = (
            "spcResourceId",
            "managedIdentityResourceId",
            "keyVaultResourceId",
        )
        for name in secretsync_result["sites"]:
            step1 = find_step(secretsync_result, name, "secretsync")
            step2 = find_step(result2, name, "secretsync")
            for output_name in stable_outputs:
                v1 = assert_output_exists(step1, output_name)
                v2 = assert_output_exists(step2, output_name)
                assert v1 == v2, (
                    f"Site '{name}': {output_name} changed on redeploy "
                    f"({v1!r} -> {v2!r})"
                )
