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
        resolve_aio = workspace / "templates" / "common" / "resolve-aio.bicep"
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
        """aio-instance-chaining.yaml should only reference steps that exist in aio-install.yaml."""
        chaining_file = workspace / "parameters" / "aio-instance-chaining.yaml"
        refs = self._get_chaining_refs(chaining_file)

        if not refs:
            return

        aio_steps = self._get_manifest_step_names(workspace / "manifests" / "aio-install.yaml")

        for step_name, output_path, raw in refs:
            assert step_name in aio_steps, (
                f"aio-instance-chaining.yaml references unknown step '{step_name}': {raw}"
            )

    def test_aio_instance_outputs_refs_in_aio_install(self, workspace):
        """aio-instance-outputs.yaml should only reference steps that exist in aio-install.yaml."""
        chaining_file = workspace / "parameters" / "aio-instance-outputs.yaml"
        refs = self._get_chaining_refs(chaining_file)

        if not refs:
            return

        aio_steps = self._get_manifest_step_names(workspace / "manifests" / "aio-install.yaml")

        for step_name, output_path, raw in refs:
            assert step_name in aio_steps, (
                f"aio-instance-outputs.yaml references unknown step '{step_name}': {raw}"
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


class TestUpdateInstanceDispatch:
    """Ensure callers of update-instance.bicep pass every param the router declares.

    Adding a new param to the shared UPDATE primitive without wiring it into
    every caller would silently omit the value at deploy time — all params
    have defaults in the caller signature via ARM, meaning the original
    property would be wiped on PUT without any test failure. This structural
    check is cheap insurance against that class of regression.
    """

    PARAM_DECL_RE = re.compile(
        r"^\s*param\s+(\w+)\s+(\w+|\w+\?)", re.MULTILINE
    )

    def _router_params(self, workspace: Path) -> set[str]:
        bicep = (
            workspace / "templates" / "aio" / "modules" / "update-instance.bicep"
        ).read_text(encoding="utf-8")
        return {m.group(1) for m in self.PARAM_DECL_RE.finditer(bicep)}

    def _caller_module_params(self, caller_path: Path) -> set[str]:
        """Parse the `params: { ... }` block of the first `../aio/modules/update-instance.bicep`
        module invocation in the caller. The containing module block may embed
        `${...}` interpolation in `name:` so the outer regex uses lazy `.*?` with
        DOTALL rather than a negated-brace class."""
        text = caller_path.read_text(encoding="utf-8")
        module_re = re.compile(
            r"module\s+\w+\s+'[^']*update-instance\.bicep'\s*=\s*\{"
            r".*?params:\s*\{(.*?)^\s*\}",
            re.DOTALL | re.MULTILINE,
        )
        m = module_re.search(text)
        assert m, f"{caller_path.name}: no update-instance.bicep module invocation found"
        body = m.group(1)
        return set(re.findall(r"^\s*(\w+)\s*:", body, re.MULTILINE))

    def test_enable_secretsync_passes_all_router_params(self, workspace):
        router = self._router_params(workspace)
        caller = self._caller_module_params(
            workspace / "templates" / "secretsync" / "enable-secretsync.bicep"
        )
        missing = router - caller
        assert missing == set(), (
            f"enable-secretsync.bicep does not forward these update-instance "
            f"router params: {sorted(missing)}. Every param on "
            f"templates/aio/modules/update-instance.bicep must be passed, or "
            f"the corresponding instance property will be wiped on PUT."
        )
        extra = caller - router
        assert extra == set(), (
            f"enable-secretsync.bicep passes params not declared by the "
            f"update-instance router: {sorted(extra)}. Remove them or add "
            f"them to templates/aio/modules/update-instance.bicep."
        )



# Required fields in every version config file
VERSION_CONFIG_REQUIRED_FIELDS = {
    "aioVersion",
    "aioTrain",
    "aioApiVersion",
    "certManagerVersion",
    "certManagerTrain",
    "secretStoreVersion",
    "secretStoreTrain",
}


class TestVersionConfigs:
    """Version config YAML files should be valid and consistent."""

    def _get_version_files(self, workspace: Path) -> list[Path]:
        versions_dir = workspace / "parameters" / "aio-versions"
        return sorted(versions_dir.glob("*.yaml"))

    def test_version_files_exist(self, workspace):
        """At least one version config should exist."""
        files = self._get_version_files(workspace)
        assert len(files) >= 1, "No version config files found in parameters/aio-versions/"

    def test_version_configs_have_required_fields(self, workspace):
        """Every version config must have all required fields."""
        for version_file in self._get_version_files(workspace):
            with open(version_file, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f)

            actual_keys = set(config.keys())
            missing = VERSION_CONFIG_REQUIRED_FIELDS - actual_keys
            assert missing == set(), (
                f"{version_file.name} missing required fields: {missing}"
            )

    def test_version_config_values_are_non_empty(self, workspace):
        """All version config values must be non-empty strings."""
        for version_file in self._get_version_files(workspace):
            with open(version_file, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f)

            for key in VERSION_CONFIG_REQUIRED_FIELDS:
                value = config.get(key)
                assert value is not None and str(value).strip() != "", (
                    f"{version_file.name}: '{key}' is empty or missing"
                )

    def test_base_site_aio_version_has_config_file(self, workspace):
        """The aioVersion in base-site.yaml must have a matching config file."""
        base_path = workspace / "sites" / "base-site.yaml"
        with open(base_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        aio_version = data.get("properties", {}).get("aioVersion")
        assert aio_version, "base-site.yaml missing properties.aioVersion"

        version_file = workspace / "parameters" / "aio-versions" / f"{aio_version}.yaml"
        assert version_file.exists(), (
            f"base-site.yaml references aioVersion '{aio_version}' "
            f"but parameters/aio-versions/{aio_version}.yaml does not exist"
        )

    def test_all_sites_aio_versions_have_config_files(self, workspace):
        """Every committed site that pins an aioVersion must reference an existing config file.

        Catches drift where a site is added or updated to use a version whose YAML
        was never created (e.g., typo, or deleted version without migrating sites).
        """
        versions_dir = workspace / "parameters" / "aio-versions"
        sites_dir = workspace / "sites"
        if not sites_dir.exists():
            return

        for site_file in sorted(sites_dir.glob("*.yaml")):
            with open(site_file, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            aio_version = (data.get("properties") or {}).get("aioVersion")
            if not aio_version:
                continue
            version_file = versions_dir / f"{aio_version}.yaml"
            assert version_file.exists(), (
                f"{site_file.name} references aioVersion '{aio_version}' "
                f"but parameters/aio-versions/{aio_version}.yaml does not exist"
            )

    def test_version_config_api_versions_are_allowed_in_bicep(self, workspace):
        """Every aioApiVersion must appear in the @allowed list of the dispatching bicep templates.

        Single source of truth: the @allowed([...]) block in templates/aio/instance.bicep
        and templates/aio/modules/update-instance.bicep. Prevents shipping a version YAML
        whose aioApiVersion the templates cannot route to (which would only surface at
        deploy time as an opaque Bicep parameter error).
        """
        dispatchers = [
            workspace / "templates" / "aio" / "instance.bicep",
            workspace / "templates" / "aio" / "modules" / "update-instance.bicep",
        ]

        # Extract the @allowed([...]) block immediately preceding `param aioApiVersion`.
        # Matches:  @allowed([\n  '2025-10-01'\n  '2026-03-01'\n])\nparam aioApiVersion
        allowed_block_re = re.compile(
            r"@allowed\(\s*\[([^\]]*)\]\s*\)\s*param\s+aioApiVersion\b",
            re.MULTILINE,
        )
        literal_re = re.compile(r"'([^']+)'")

        def extract_allowed(bicep_path: Path) -> set[str]:
            text = bicep_path.read_text(encoding="utf-8")
            match = allowed_block_re.search(text)
            assert match, f"{bicep_path.name}: could not find @allowed block before `param aioApiVersion`"
            return set(literal_re.findall(match.group(1)))

        allowed_sets = {p.name: extract_allowed(p) for p in dispatchers}
        # Sanity: both dispatchers must agree on the allowed set.
        values = list(allowed_sets.values())
        assert all(s == values[0] for s in values), (
            f"@allowed lists for aioApiVersion diverge between dispatchers: {allowed_sets}"
        )
        allowed = values[0]
        assert allowed, "No @allowed values parsed — regex or template changed"

        for version_file in self._get_version_files(workspace):
            with open(version_file, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f)
            api_version = config.get("aioApiVersion")
            assert api_version in allowed, (
                f"{version_file.name}: aioApiVersion '{api_version}' is not in the "
                f"@allowed set {sorted(allowed)} declared by "
                f"{', '.join(sorted(allowed_sets.keys()))}. "
                f"Add the new API version to both dispatchers' @allowed blocks and "
                f"their ternary dispatch before shipping this version YAML."
            )
