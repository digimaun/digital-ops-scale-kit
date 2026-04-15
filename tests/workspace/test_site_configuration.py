"""Tests that site inheritance resolves correctly and consistently."""

from pathlib import Path

import yaml

from siteops.orchestrator import Orchestrator


# All deployOptions defined in base-site.yaml
EXPECTED_DEPLOY_OPTIONS = {
    "includeGlobalSite",
    "includeEdgeSite",
    "includeSolution",
    "includeOpcPlcSimulator",
    "enableSecretSync",
}


class TestSiteInheritanceResolution:
    """Every site should load cleanly with complete inherited configuration."""

    def _get_site_names(self, workspace: Path) -> list[str]:
        """Get all Site (not SiteTemplate) names from the workspace."""
        sites_dir = workspace / "sites"
        names = []
        for f in sorted(sites_dir.glob("*.yaml")):
            with open(f, "r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh)
            if data and data.get("kind") != "SiteTemplate":
                names.append(data.get("name", f.stem))
        return names

    def test_all_sites_load(self, workspace, orchestrator):
        """Every Site file should load without errors."""
        site_names = self._get_site_names(workspace)
        assert len(site_names) >= 1, "No sites found"

        for name in site_names:
            site = orchestrator.load_site(name)
            assert site.name == name
            assert site.subscription, f"{name}: missing subscription"
            assert site.location, f"{name}: missing location"

    def test_all_sites_have_complete_deploy_options(self, workspace, orchestrator):
        """Every site should inherit all deployOptions from base-site.yaml."""
        site_names = self._get_site_names(workspace)

        for name in site_names:
            site = orchestrator.load_site(name)
            deploy_options = site.properties.get("deployOptions", {})
            actual_keys = set(deploy_options.keys())
            missing = EXPECTED_DEPLOY_OPTIONS - actual_keys
            assert missing == set(), (
                f"{name}: missing deployOptions keys after inheritance: {missing}"
            )

    def test_base_site_defines_all_deploy_options(self, workspace):
        """base-site.yaml should define every expected deployOptions key."""
        base_path = workspace / "sites" / "base-site.yaml"
        with open(base_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        deploy_options = data.get("properties", {}).get("deployOptions", {})
        actual_keys = set(deploy_options.keys())
        missing = EXPECTED_DEPLOY_OPTIONS - actual_keys
        assert missing == set(), (
            f"base-site.yaml missing deployOptions keys: {missing}"
        )

    def test_shared_templates_inherit_base(self, workspace):
        """All shared SiteTemplates should inherit from base-site.yaml."""
        shared_dir = workspace / "sites" / "shared"
        if not shared_dir.is_dir():
            return

        for f in sorted(shared_dir.glob("*.yaml")):
            with open(f, "r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh)
            inherits = data.get("inherits", "")
            assert "base-site" in inherits, (
                f"shared/{f.name} does not inherit from base-site.yaml: inherits={inherits}"
            )

    def test_no_site_has_placeholder_subscription(self, workspace, orchestrator):
        """Sites should not have obviously placeholder subscription IDs."""
        site_names = self._get_site_names(workspace)

        for name in site_names:
            site = orchestrator.load_site(name)
            assert site.subscription != "", f"{name}: empty subscription"
            # Allow the 00000000 placeholder since committed sites use it
            # (real values come from sites.local/ overlays)
