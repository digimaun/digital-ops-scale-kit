"""Workspace contracts for optional extensions in the AIO upgrade flow."""

from pathlib import Path

import yaml


def _read(workspace: Path, relative_path: str) -> str:
    return (workspace / relative_path).read_text(encoding="utf-8")


class TestAioUpgradeSecretSyncOptionality:
    """Secret Sync disabled skips azure-secret-store reads and writes."""

    def test_enable_secret_sync_is_chained_to_both_extension_steps(self, workspace):
        for relative_path in (
            "parameters/inputs/aio-upgrade-resolve-extensions.yaml",
            "parameters/inputs/aio-upgrade-update-extensions.yaml",
        ):
            data = yaml.safe_load(_read(workspace, relative_path))
            assert data["enableSecretSync"] == (
                "{{ site.properties.deployOptions.enableSecretSync }}"
            )

    def test_disabled_path_skips_secret_store_lookup(self, workspace):
        text = _read(
            workspace, "templates/aio/upgrade/resolve-extensions.bicep"
        )
        assert (
            "resource secretStoreExtension "
            "'Microsoft.KubernetesConfiguration/extensions@2023-05-01' "
            "existing = if (enableSecretSync)"
        ) in text
        output_start = text.index("output secretStore object = enableSecretSync")
        output_end = text.index("@description('cert-manager", output_start)
        output = text[output_start:output_end]
        for disabled_field in (
            "id: ''",
            "name: secretStoreExtensionName",
            "extensionType: secretStoreExtensionType",
            "version: ''",
            "releaseTrain: ''",
            "configurationSettings: {}",
            "identity: { type: 'None' }",
        ):
            assert disabled_field in output

    def test_disabled_path_skips_secret_store_put(self, workspace):
        text = _read(
            workspace, "templates/aio/upgrade/update-extensions.bicep"
        )
        assert (
            "resource secretStoreExtensionUpdate "
            "'Microsoft.KubernetesConfiguration/extensions@2023-05-01' "
            "= if (enableSecretSync)"
        ) in text
        assert (
            "output secretStoreExtensionId string = enableSecretSync "
            "? secretStoreExtensionUpdate!.id : ''"
        ) in text
        assert "output secretStorePostUpdate object = enableSecretSync" in text

    def test_enabled_path_preserves_secret_store_contract(self, workspace):
        text = _read(
            workspace, "templates/aio/upgrade/update-extensions.bicep"
        )
        resource_start = text.index("resource secretStoreExtensionUpdate")
        resource_end = text.index(
            "// cert-manager Extension", resource_start
        )
        resource = text[resource_start:resource_end]
        for preserved_field in (
            "identity: secretStore.identity",
            "version: effectiveSecretStoreVersion",
            "releaseTrain: effectiveSecretStoreTrain",
            (
                "configurationSettings: union("
                "secretStore.configurationSettings, "
                "secretStoreConfigurationOverrides)"
            ),
        ):
            assert preserved_field in resource


class TestAioUpgradeOidcOptionality:
    """Cluster resolution tolerates workload identity being disabled."""

    def test_cluster_oidc_outputs_are_safe_when_profile_is_absent(self, workspace):
        text = _read(
            workspace, "templates/common/modules/resolve-cluster.bicep"
        )
        assert (
            "connectedCluster.properties.?oidcIssuerProfile.?issuerUrl ?? ''"
        ) in text
        assert (
            "connectedCluster.properties.?oidcIssuerProfile."
            "?selfHostedIssuerUrl ?? ''"
        ) in text

    def test_resolve_aio_keeps_the_oidc_output_contract(self, workspace):
        text = _read(workspace, "templates/aio/resolve-aio.bicep")
        assert "output oidcIssuerUrl string = useSelfHostedIssuer" in text
        assert "resolvedCluster.outputs.selfHostedIssuerUrl" in text
        assert "resolvedCluster.outputs.oidcIssuerUrl" in text

    def test_oidc_presence_does_not_enable_secret_store_updates(self, workspace):
        resolve_cluster = _read(
            workspace, "templates/common/modules/resolve-cluster.bicep"
        )
        resolve_extensions = _read(
            workspace, "templates/aio/upgrade/resolve-extensions.bicep"
        )
        update_extensions = _read(
            workspace, "templates/aio/upgrade/update-extensions.bicep"
        )
        assert "enableSecretSync" not in resolve_cluster
        assert "oidcIssuerProfile" not in resolve_extensions
        assert "oidcIssuerProfile" not in update_extensions
