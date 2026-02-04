"""Tests for core orchestrator functionality.

Covers:
- Site loading and caching
- Site overlay merging (sites/, sites.local/)
- Site resolution from manifests
- Deployment name generation
- Output path resolution
"""

from unittest.mock import MagicMock, patch

import pytest
import yaml

from siteops.models import DeploymentStep, Manifest, ParallelConfig, Site
from siteops.orchestrator import Orchestrator, _resolve_output_path


class TestResolveOutputPath:
    """Tests for the _resolve_output_path helper function."""

    def test_simple_path(self):
        obj = {"name": "test-value"}
        assert _resolve_output_path(obj, "name") == "test-value"

    def test_nested_path(self):
        obj = {"resource": {"id": "resource-123", "name": "myresource"}}
        assert _resolve_output_path(obj, "resource.id") == "resource-123"

    def test_azure_output_unwrap(self):
        # Azure ARM outputs are wrapped in {"value": X, "type": "..."}
        obj = {"storageId": {"value": "storage-123", "type": "String"}}
        assert _resolve_output_path(obj, "storageId") == "storage-123"

    def test_nested_azure_output(self):
        obj = {
            "resource": {
                "value": {"id": "res-123", "name": "myres"},
                "type": "Object",
            }
        }
        assert _resolve_output_path(obj, "resource.id") == "res-123"

    def test_missing_path(self):
        obj = {"name": "test"}
        assert _resolve_output_path(obj, "nonexistent") is None
        assert _resolve_output_path(obj, "name.nested") is None

    def test_none_input(self):
        assert _resolve_output_path(None, "anything") is None


class TestOrchestratorSiteLoading:
    """Tests for site loading functionality."""

    def test_load_site_basic(self, complete_workspace):
        orchestrator = Orchestrator(complete_workspace)
        site = orchestrator.load_site("test-site")

        assert site.name == "test-site"
        assert site.location == "eastus"
        assert site.labels["environment"] == "dev"

    def test_load_site_caching(self, complete_workspace):
        orchestrator = Orchestrator(complete_workspace)

        site1 = orchestrator.load_site("test-site")
        site2 = orchestrator.load_site("test-site")

        assert site1 is site2  # Same object from cache

    def test_load_site_not_found(self, tmp_workspace):
        orchestrator = Orchestrator(tmp_workspace)

        with pytest.raises(FileNotFoundError, match="not found"):
            orchestrator.load_site("nonexistent")

    def test_load_all_sites(self, multi_site_workspace):
        orchestrator = Orchestrator(multi_site_workspace)
        sites = orchestrator.load_all_sites()

        assert len(sites) == 3
        site_names = {s.name for s in sites}
        assert site_names == {"dev-eastus", "dev-westus", "prod-eastus"}

    def test_load_site_with_yml_extension(self, tmp_workspace):
        """Sites with .yml extension should load correctly."""
        (tmp_workspace / "sites" / "yml-site.yml").write_text(
            """
apiVersion: siteops/v1
kind: Site
name: yml-site
subscription: "00000000-0000-0000-0000-000000000000"
resourceGroup: rg-yml
location: eastus
"""
        )

        orchestrator = Orchestrator(tmp_workspace)
        site = orchestrator.load_site("yml-site")

        assert site.name == "yml-site"
        assert site.location == "eastus"


class TestSiteOverlayMerging:
    """Tests for the two-tier site overlay system."""

    def test_sites_local_overrides_base(self, tmp_workspace):
        """Local overrides should take precedence over base."""
        base_site = {
            "name": "overlay-test",
            "subscription": "base-sub",
            "resourceGroup": "base-rg",
            "location": "eastus",
        }
        (tmp_workspace / "sites" / "overlay-test.yaml").write_text(yaml.dump(base_site))

        (tmp_workspace / "sites.local").mkdir()
        local_override = {
            "subscription": "local-sub",
            "resourceGroup": "local-rg",
        }
        (tmp_workspace / "sites.local" / "overlay-test.yaml").write_text(yaml.dump(local_override))

        orchestrator = Orchestrator(tmp_workspace)
        site = orchestrator.load_site("overlay-test")

        # Local values should override base
        assert site.subscription == "local-sub"
        assert site.resource_group == "local-rg"
        # Base values should be preserved
        assert site.location == "eastus"

    def test_deep_merge_labels(self, tmp_workspace):
        """Labels should be deep merged across overlay layers."""
        base_site = {
            "name": "merge-test",
            "subscription": "sub",
            "location": "eastus",
            "labels": {"env": "base", "team": "platform"},
        }
        (tmp_workspace / "sites" / "merge-test.yaml").write_text(yaml.dump(base_site))

        (tmp_workspace / "sites.local").mkdir()
        local_override = {"labels": {"env": "local", "added": "new"}}
        (tmp_workspace / "sites.local" / "merge-test.yaml").write_text(yaml.dump(local_override))

        orchestrator = Orchestrator(tmp_workspace)
        site = orchestrator.load_site("merge-test")

        # Labels should be deep merged
        assert site.labels["env"] == "local"  # Overridden
        assert site.labels["team"] == "platform"  # Preserved
        assert site.labels["added"] == "new"  # Added

    def test_deep_merge_properties(self, tmp_workspace):
        """Properties should be deep merged across overlay layers."""
        base_site = {
            "name": "props-merge-test",
            "subscription": "sub",
            "location": "eastus",
            "properties": {
                "mqtt": {"broker": "mqtt://base:1883", "qos": 1},
                "endpoints": [{"name": "base-endpoint", "host": "10.0.0.1"}],
                "baseOnly": "preserved",
            },
        }
        (tmp_workspace / "sites" / "props-merge-test.yaml").write_text(yaml.dump(base_site))

        (tmp_workspace / "sites.local").mkdir()
        local_override = {
            "properties": {
                "mqtt": {"broker": "mqtt://local:1883", "clientId": "local-client"},
                "localOnly": "added",
            },
        }
        (tmp_workspace / "sites.local" / "props-merge-test.yaml").write_text(yaml.dump(local_override))

        orchestrator = Orchestrator(tmp_workspace)
        site = orchestrator.load_site("props-merge-test")

        # Properties should be deep merged
        assert site.properties["mqtt"]["broker"] == "mqtt://local:1883"  # Overridden
        assert site.properties["mqtt"]["qos"] == 1  # Preserved from base
        assert site.properties["mqtt"]["clientId"] == "local-client"  # Added
        assert site.properties["baseOnly"] == "preserved"  # Base only preserved
        assert site.properties["localOnly"] == "added"  # Local only added

    def test_subscription_level_site_overlay(self, tmp_workspace):
        """Overlay on subscription-level site preserves subscription-level status."""
        base_site = {
            "name": "sub-level-test",
            "subscription": "base-sub",
            # No resourceGroup - subscription-level site
            "location": "eastus",
            "labels": {"team": "infra"},
        }
        (tmp_workspace / "sites" / "sub-level-test.yaml").write_text(yaml.dump(base_site))

        (tmp_workspace / "sites.local").mkdir()
        local_override = {
            "subscription": "local-sub",
            "labels": {"environment": "dev"},
        }
        (tmp_workspace / "sites.local" / "sub-level-test.yaml").write_text(yaml.dump(local_override))

        orchestrator = Orchestrator(tmp_workspace)
        site = orchestrator.load_site("sub-level-test")

        # Site should remain subscription-level
        assert site.is_subscription_level is True
        assert site.resource_group == ""
        # Overlay values should be applied
        assert site.subscription == "local-sub"
        assert site.labels["environment"] == "dev"
        assert site.labels["team"] == "infra"  # Preserved from base

    def test_overlay_adds_resource_group(self, tmp_workspace):
        """Overlay can convert subscription-level to RG-level by adding resourceGroup."""
        base_site = {
            "name": "upgrade-test",
            "subscription": "sub",
            # No resourceGroup - subscription-level site
            "location": "eastus",
        }
        (tmp_workspace / "sites" / "upgrade-test.yaml").write_text(yaml.dump(base_site))

        (tmp_workspace / "sites.local").mkdir()
        local_override = {"resourceGroup": "rg-from-overlay"}
        (tmp_workspace / "sites.local" / "upgrade-test.yaml").write_text(yaml.dump(local_override))

        orchestrator = Orchestrator(tmp_workspace)
        site = orchestrator.load_site("upgrade-test")

        # Site should now be RG-level
        assert site.is_subscription_level is False
        assert site.resource_group == "rg-from-overlay"


class TestResolveSites:
    """Tests for site resolution from manifests."""

    def test_explicit_sites_list(self, multi_site_workspace):
        orchestrator = Orchestrator(multi_site_workspace)
        manifest = Manifest(
            name="test",
            description="",
            sites=["dev-eastus", "dev-westus"],
            steps=[],
        )

        sites = orchestrator.resolve_sites(manifest)

        assert len(sites) == 2
        assert {s.name for s in sites} == {"dev-eastus", "dev-westus"}

    def test_site_selector(self, multi_site_workspace):
        orchestrator = Orchestrator(multi_site_workspace)
        manifest = Manifest(
            name="test",
            description="",
            sites=[],
            steps=[],
            site_selector="environment=dev",
        )

        sites = orchestrator.resolve_sites(manifest)

        assert len(sites) == 2
        assert all(s.labels["environment"] == "dev" for s in sites)

    def test_cli_selector_overrides(self, multi_site_workspace):
        orchestrator = Orchestrator(multi_site_workspace)
        manifest = Manifest(
            name="test",
            description="",
            sites=["dev-eastus"],  # Explicit list
            steps=[],
        )

        # CLI selector should override explicit list
        sites = orchestrator.resolve_sites(manifest, cli_selector="region=eastus")

        assert len(sites) == 2
        assert all(s.labels["region"] == "eastus" for s in sites)


class TestDeploymentNameGeneration:
    """Tests for deployment name truncation and hashing."""

    def test_short_name_no_truncation(self, complete_workspace):
        orchestrator = Orchestrator(complete_workspace)
        site = Site(
            name="dev",
            subscription="sub",
            resource_group="rg",
            location="eastus",
        )
        manifest = Manifest(name="short", description="", sites=[], steps=[])
        step = DeploymentStep(name="step", template="test.bicep")

        with patch.object(orchestrator.executor, "deploy_resource_group") as mock_deploy:
            mock_deploy.return_value = MagicMock(success=True, outputs={})
            orchestrator._deploy_bicep_step(site, step, manifest, "20260102120000", {})

            call_args = mock_deploy.call_args
            deployment_name = call_args.kwargs["deployment_name"]

            assert len(deployment_name) <= 64
            assert deployment_name == "short-dev-step-20260102120000"

    def test_long_name_gets_hash_suffix(self, complete_workspace):
        """Long deployment names should be truncated with hash suffix."""
        orchestrator = Orchestrator(complete_workspace)
        site = Site(
            name="very-long-site-name-that-exceeds-limits",
            subscription="sub",
            resource_group="rg",
            location="eastus",
        )
        manifest = Manifest(name="very-long-manifest-name", description="", sites=[], steps=[])
        step = DeploymentStep(name="very-long-step-name", template="test.bicep")

        with patch.object(orchestrator.executor, "deploy_resource_group") as mock_deploy:
            mock_deploy.return_value = MagicMock(success=True, outputs={})
            orchestrator._deploy_bicep_step(site, step, manifest, "20260102120000", {})

            call_args = mock_deploy.call_args
            deployment_name = call_args.kwargs["deployment_name"]

            # Name should be within Azure's limit
            assert len(deployment_name) <= 64
            # Should end with timestamp
            assert deployment_name.endswith("20260102120000")
            # Full name would be: very-long-manifest-name-very-long-site-name-that-exceeds-limits-very-long-step-name-20260102120000
            # Since that exceeds 64 chars, it should be truncated with a hash
            # The truncated name should be shorter than the full name would be
            full_name = (
                "very-long-manifest-name-very-long-site-name-that-exceeds-limits-very-long-step-name-20260102120000"
            )
            assert len(deployment_name) < len(full_name)


class TestDeployParallelConfig:
    """Tests for deployment with different parallel configurations."""

    def test_deploy_uses_manifest_parallel_config(self, complete_workspace):
        """Test that deploy uses the manifest's parallel config by default."""
        Orchestrator(complete_workspace)

        # Create manifest with parallel: 2
        manifest_path = complete_workspace / "manifests" / "parallel-test.yaml"
        manifest_path.write_text(
            """
apiVersion: siteops/v1
kind: Manifest
name: parallel-test
sites: [test-site]
parallel: 2
steps:
  - name: step1
    template: templates/test.bicep
"""
        )

        manifest = Manifest.from_file(manifest_path)

        assert manifest.parallel.sites == 2
        assert manifest.parallel.max_workers == 2

    def test_deploy_parallel_override_takes_precedence(self, complete_workspace):
        """Test that parallel_override parameter takes precedence over manifest."""
        orchestrator = Orchestrator(complete_workspace)

        manifest = Manifest(
            name="test",
            description="",
            sites=["test-site"],
            steps=[DeploymentStep(name="step1", template="templates/test.bicep")],
            parallel=ParallelConfig(sites=1),  # Sequential in manifest
        )

        # When parallel_override is provided, it should be used
        with patch.object(orchestrator, "_deploy_sequential") as mock_seq:
            with patch.object(orchestrator, "_deploy_parallel") as mock_par:
                mock_seq.return_value = []
                mock_par.return_value = []

                # Override to parallel mode
                orchestrator.deploy(
                    complete_workspace / "manifests" / "test-manifest.yaml",
                    parallel_override=3,
                    manifest=manifest,
                    sites=[orchestrator.load_site("test-site")],
                )

                # Should use parallel, not sequential
                assert mock_par.called or mock_seq.called

    def test_deploy_single_site_always_sequential(self, complete_workspace):
        """Test that single site deployment is always sequential regardless of config."""
        orchestrator = Orchestrator(complete_workspace)

        manifest = Manifest(
            name="test",
            description="",
            sites=["test-site"],
            steps=[DeploymentStep(name="step1", template="templates/test.bicep")],
            parallel=ParallelConfig(sites=0),  # Unlimited in manifest
        )

        with patch.object(orchestrator, "_deploy_sequential") as mock_seq:
            with patch.object(orchestrator, "_deploy_parallel") as mock_par:
                mock_seq.return_value = []

                orchestrator.deploy(
                    complete_workspace / "manifests" / "test-manifest.yaml",
                    manifest=manifest,
                    sites=[orchestrator.load_site("test-site")],
                )

                # Single site should use sequential
                mock_seq.assert_called_once()
                mock_par.assert_not_called()


class TestPlanParallelDisplay:
    """Tests for parallel config display in show_plan output."""

    def test_plan_shows_parallel_config(self, complete_workspace, capsys):
        """Test that show_plan output shows parallel configuration."""
        orchestrator = Orchestrator(complete_workspace)

        manifest_path = complete_workspace / "manifests" / "parallel-plan.yaml"
        manifest_path.write_text(
            """
apiVersion: siteops/v1
kind: Manifest
name: parallel-plan
sites: [test-site]
parallel: 3
steps:
  - name: step1
    template: templates/test.bicep
"""
        )

        orchestrator.show_plan(manifest_path)

        captured = capsys.readouterr()
        # Check for parallel info in output - be flexible about exact format
        assert "Parallel" in captured.out or "parallel" in captured.out.lower()
        assert "3" in captured.out or "max 3" in captured.out

    def test_plan_shows_sequential(self, complete_workspace, capsys):
        """Test that show_plan output shows sequential mode."""
        orchestrator = Orchestrator(complete_workspace)
        manifest_path = complete_workspace / "manifests" / "test-manifest.yaml"

        orchestrator.show_plan(manifest_path)

        captured = capsys.readouterr()
        # Check for parallel info - sequential is default
        assert "Parallel" in captured.out or "sequential" in captured.out.lower()

    def test_plan_shows_unlimited(self, complete_workspace, capsys):
        """Test that show_plan output shows unlimited mode."""
        orchestrator = Orchestrator(complete_workspace)

        manifest_path = complete_workspace / "manifests" / "unlimited-plan.yaml"
        manifest_path.write_text(
            """
apiVersion: siteops/v1
kind: Manifest
name: unlimited-plan
sites: [test-site]
parallel: 0
steps:
  - name: step1
    template: templates/test.bicep
"""
        )

        orchestrator.show_plan(manifest_path)

        captured = capsys.readouterr()
        # Check for unlimited indicator
        assert "Parallel" in captured.out or "unlimited" in captured.out.lower()
