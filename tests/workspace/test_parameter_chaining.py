"""Tests that parameter chaining files reference valid step outputs."""

import re
from pathlib import Path

import yaml


# Pattern to extract step references: {{ steps.<step_name>.outputs.<path> }}
STEP_OUTPUT_PATTERN = re.compile(r"\{\{\s*steps\.([^.]+)\.outputs\.(\S+?)\s*\}\}")


class TestParameterChaining:
    """Chaining parameter files should reference steps and outputs that exist."""

    def _get_chaining_refs(self, param_file: Path) -> list[tuple[str, str, str]]:
        """Extract (step_name, output_path, raw_template) from a parameter file."""
        with open(param_file, "r", encoding="utf-8") as f:
            content = f.read()

        refs = []
        for match in STEP_OUTPUT_PATTERN.finditer(content):
            step_name = match.group(1)
            output_path = match.group(2)
            refs.append((step_name, output_path, match.group(0)))
        return refs

    def _get_manifest_step_names(self, manifest_path: Path) -> set[str]:
        """Get all step names from a manifest."""
        from siteops.models import Manifest
        manifest = Manifest.from_file(manifest_path)
        return {s.name for s in manifest.steps}

    def test_secretsync_chaining_refs_valid_steps(self, workspace):
        """secretsync-chaining.yaml should only reference steps that exist in manifests."""
        chaining_file = workspace / "parameters" / "secretsync-chaining.yaml"
        refs = self._get_chaining_refs(chaining_file)
        assert len(refs) > 0, "No step output references found in secretsync-chaining.yaml"

        # Get step names from both manifests that use this chaining file
        aio_steps = self._get_manifest_step_names(workspace / "manifests" / "aio-install.yaml")
        secretsync_steps = self._get_manifest_step_names(workspace / "manifests" / "secretsync.yaml")
        all_valid_steps = aio_steps | secretsync_steps

        for step_name, output_path, raw in refs:
            assert step_name in all_valid_steps, (
                f"secretsync-chaining.yaml references unknown step '{step_name}': {raw}"
            )

    def test_secretsync_chaining_refs_valid_outputs(self, workspace):
        """Every output referenced in secretsync-chaining.yaml should exist in resolve-aio.bicep."""
        chaining_file = workspace / "parameters" / "secretsync-chaining.yaml"
        refs = self._get_chaining_refs(chaining_file)

        # Parse output names from resolve-aio.bicep
        resolve_aio = workspace / "templates" / "iot-ops" / "common" / "resolve-aio.bicep"
        bicep_content = resolve_aio.read_text(encoding="utf-8")
        output_names = set(re.findall(r"^output\s+(\w+)\s+", bicep_content, re.MULTILINE))
        assert len(output_names) > 0, "No outputs found in resolve-aio.bicep"

        for step_name, output_path, raw in refs:
            if step_name != "resolve-aio":
                continue
            # The top-level output name is the first segment of the path
            top_level_output = output_path.split(".")[0]
            assert top_level_output in output_names, (
                f"secretsync-chaining.yaml references unknown output "
                f"'{top_level_output}' from resolve-aio: {raw}\n"
                f"Available outputs: {sorted(output_names)}"
            )

    def test_chaining_yaml_refs_in_aio_install(self, workspace):
        """chaining.yaml should only reference steps that exist in aio-install.yaml."""
        chaining_file = workspace / "parameters" / "chaining.yaml"
        refs = self._get_chaining_refs(chaining_file)

        if not refs:
            return

        aio_steps = self._get_manifest_step_names(workspace / "manifests" / "aio-install.yaml")

        for step_name, output_path, raw in refs:
            assert step_name in aio_steps, (
                f"chaining.yaml references unknown step '{step_name}': {raw}"
            )

    def test_post_instance_yaml_refs_in_aio_install(self, workspace):
        """post-instance.yaml should only reference steps that exist in aio-install.yaml."""
        chaining_file = workspace / "parameters" / "post-instance.yaml"
        refs = self._get_chaining_refs(chaining_file)

        if not refs:
            return

        aio_steps = self._get_manifest_step_names(workspace / "manifests" / "aio-install.yaml")

        for step_name, output_path, raw in refs:
            assert step_name in aio_steps, (
                f"post-instance.yaml references unknown step '{step_name}': {raw}"
            )


class TestConditionalStepCoverage:
    """Every when: condition should reference a property that exists in base-site.yaml."""

    def _get_conditions_from_manifest(self, manifest_path: Path) -> list[tuple[str, str]]:
        """Extract (step_name, condition) pairs from a manifest."""
        from siteops.models import Manifest
        manifest = Manifest.from_file(manifest_path)
        conditions = []
        for step in manifest.steps:
            if step.when:
                conditions.append((step.name, step.when))
        return conditions

    def _get_base_site_property_paths(self, workspace: Path) -> set[str]:
        """Get all dot-separated property paths defined in base-site.yaml."""
        base_path = workspace / "sites" / "base-site.yaml"
        with open(base_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        paths = set()
        properties = data.get("properties", {})

        def collect_paths(d: dict, prefix: str = ""):
            for k, v in d.items():
                full = f"{prefix}.{k}" if prefix else k
                paths.add(full)
                if isinstance(v, dict):
                    collect_paths(v, full)

        collect_paths(properties)
        return paths

    def test_all_when_conditions_reference_known_properties(self, workspace):
        """Every when: condition property path should exist in base-site.yaml."""
        known_paths = self._get_base_site_property_paths(workspace)
        prop_pattern = re.compile(r"site\.properties\.([\w.]+)")

        manifests_dir = workspace / "manifests"
        for manifest_file in sorted(manifests_dir.glob("*.yaml")):
            conditions = self._get_conditions_from_manifest(manifest_file)

            for step_name, condition in conditions:
                match = prop_pattern.search(condition)
                if not match:
                    continue

                prop_path = match.group(1)
                assert prop_path in known_paths, (
                    f"{manifest_file.name} step '{step_name}' references unknown property "
                    f"'site.properties.{prop_path}' in when condition.\n"
                    f"Known property paths: {sorted(known_paths)}"
                )
