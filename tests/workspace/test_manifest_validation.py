"""Tests that all workspace manifests pass validation."""

from pathlib import Path

from siteops.orchestrator import Orchestrator


class TestManifestValidation:
    """Every manifest in the workspace should validate without errors."""

    def _get_manifest_files(self, workspace: Path) -> list[Path]:
        manifests_dir = workspace / "manifests"
        return sorted(manifests_dir.glob("*.yaml")) + sorted(manifests_dir.glob("*.yml"))

    def test_all_manifests_discovered(self, workspace):
        """Sanity check: workspace has manifests to validate."""
        manifests = self._get_manifest_files(workspace)
        assert len(manifests) >= 1, "No manifests found in workspace"

    def test_aio_install_validates(self, workspace, orchestrator):
        """aio-install.yaml should validate with no errors."""
        errors = orchestrator.validate(workspace / "manifests" / "aio-install.yaml")
        assert errors == [], f"aio-install.yaml validation errors: {errors}"

    def test_secretsync_validates(self, workspace, orchestrator):
        """secretsync.yaml should validate with no errors."""
        errors = orchestrator.validate(workspace / "manifests" / "secretsync.yaml")
        assert errors == [], f"secretsync.yaml validation errors: {errors}"

    def test_opc_ua_solution_validates(self, workspace, orchestrator):
        """opc-ua-solution.yaml should validate with no errors."""
        errors = orchestrator.validate(workspace / "manifests" / "opc-ua-solution.yaml")
        assert errors == [], f"opc-ua-solution.yaml validation errors: {errors}"

    def test_no_duplicate_step_names_in_any_manifest(self, workspace, orchestrator):
        """No manifest should have duplicate step names."""
        from siteops.models import Manifest

        for manifest_path in self._get_manifest_files(workspace):
            manifest = Manifest.from_file(manifest_path)
            step_names = [s.name for s in manifest.steps]
            duplicates = [n for n in step_names if step_names.count(n) > 1]
            assert duplicates == [], (
                f"{manifest_path.name} has duplicate step names: {set(duplicates)}"
            )
