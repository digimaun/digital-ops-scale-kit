"""Tests for manifest validation.

Covers:
- Basic manifest validation
- Manifest-level parameter validation
- Step output reference validation
- Self-reference detection with auto-filter awareness
"""

from unittest.mock import patch

import yaml

from siteops.orchestrator import Orchestrator


class TestValidation:
    """Tests for manifest validation."""

    def test_validate_success(self, complete_workspace):
        orchestrator = Orchestrator(complete_workspace)
        manifest_path = complete_workspace / "manifests" / "test-manifest.yaml"

        errors = orchestrator.validate(manifest_path)
        assert errors == []

    def test_validate_missing_template(self, tmp_workspace, sample_site_file):
        orchestrator = Orchestrator(tmp_workspace)

        manifest_data = {
            "name": "bad-manifest",
            "sites": ["test-site"],
            "steps": [{"name": "step1", "template": "nonexistent.bicep"}],
        }
        manifest_path = tmp_workspace / "manifests" / "bad.yaml"
        with open(manifest_path, "w", encoding="utf-8") as f:
            yaml.dump(manifest_data, f)

        errors = orchestrator.validate(manifest_path)
        assert any("Template not found" in e for e in errors)

    def test_validate_missing_step_parameters(self, complete_workspace):
        """Test that missing step parameter files are caught."""
        orchestrator = Orchestrator(complete_workspace)

        manifest_data = {
            "name": "bad-manifest",
            "sites": ["test-site"],
            "steps": [
                {
                    "name": "step1",
                    "template": "templates/test.bicep",
                    "parameters": ["nonexistent.yaml"],
                }
            ],
        }
        manifest_path = complete_workspace / "manifests" / "bad.yaml"
        with open(manifest_path, "w", encoding="utf-8") as f:
            yaml.dump(manifest_data, f)

        errors = orchestrator.validate(manifest_path)
        assert any("Parameter file not found" in e for e in errors)

    def test_validate_no_sites_matched(self, complete_workspace):
        orchestrator = Orchestrator(complete_workspace)

        manifest_data = {
            "name": "no-match",
            "siteSelector": "nonexistent=value",
            "steps": [{"name": "step1", "template": "templates/test.bicep"}],
        }
        manifest_path = complete_workspace / "manifests" / "no-match.yaml"
        with open(manifest_path, "w", encoding="utf-8") as f:
            yaml.dump(manifest_data, f)

        errors = orchestrator.validate(manifest_path)
        assert any("No sites matched" in e for e in errors)

    def test_validate_invalid_condition(self, complete_workspace):
        orchestrator = Orchestrator(complete_workspace)

        manifest_data = {
            "name": "bad-condition",
            "sites": ["test-site"],
            "steps": [
                {
                    "name": "step1",
                    "template": "templates/test.bicep",
                    "when": "invalid condition syntax",
                }
            ],
        }
        manifest_path = complete_workspace / "manifests" / "bad.yaml"
        with open(manifest_path, "w", encoding="utf-8") as f:
            yaml.dump(manifest_data, f)

        errors = orchestrator.validate(manifest_path)

        assert len(errors) > 0
        assert any("when" in e.lower() or "condition" in e.lower() or "parse" in e.lower() for e in errors)

    # --- Manifest-level parameter validation tests ---

    def test_validate_manifest_parameters_exist(self, tmp_path):
        """Test that existing manifest parameter files pass validation."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "sites").mkdir()
        (workspace / "parameters").mkdir()
        (workspace / "templates").mkdir()
        (workspace / "manifests").mkdir()

        site_file = workspace / "sites" / "test-site.yaml"
        site_file.write_text(
            """
apiVersion: siteops/v1
kind: Site
name: test-site
subscription: "00000000-0000-0000-0000-000000000000"
resourceGroup: rg-test
location: eastus
"""
        )

        params_file = workspace / "parameters" / "common.yaml"
        params_file.write_text("location: eastus\nenvironment: dev\n")

        template_file = workspace / "templates" / "test.bicep"
        template_file.write_text("param location string")

        manifest_file = workspace / "manifests" / "test.yaml"
        manifest_file.write_text(
            """
apiVersion: siteops/v1
kind: Manifest
name: test
description: Test
sites:
  - test-site
parameters:
  - parameters/common.yaml
steps:
  - name: test-step
    template: templates/test.bicep
    scope: resourceGroup
"""
        )

        orchestrator = Orchestrator(workspace)
        errors = orchestrator.validate(manifest_file)

        param_errors = [e for e in errors if "Manifest parameter file" in e]
        assert param_errors == []

    def test_validate_manifest_parameters_missing_file(self, tmp_path):
        """Test that missing manifest parameter file is caught."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "sites").mkdir()
        (workspace / "templates").mkdir()
        (workspace / "manifests").mkdir()

        site_file = workspace / "sites" / "test-site.yaml"
        site_file.write_text(
            """
apiVersion: siteops/v1
kind: Site
name: test-site
subscription: "00000000-0000-0000-0000-000000000000"
resourceGroup: rg-test
location: eastus
"""
        )

        template_file = workspace / "templates" / "test.bicep"
        template_file.write_text("param location string")

        manifest_file = workspace / "manifests" / "test.yaml"
        manifest_file.write_text(
            """
apiVersion: siteops/v1
kind: Manifest
name: test
description: Test
sites:
  - test-site
parameters:
  - parameters/nonexistent.yaml
steps:
  - name: test-step
    template: templates/test.bicep
    scope: resourceGroup
"""
        )

        orchestrator = Orchestrator(workspace)
        errors = orchestrator.validate(manifest_file)

        assert any("Manifest parameter file not found" in e for e in errors)
        assert any("nonexistent.yaml" in e for e in errors)

    def test_validate_manifest_parameters_invalid_yaml(self, tmp_path):
        """Test that invalid YAML in manifest parameter file is caught."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "sites").mkdir()
        (workspace / "parameters").mkdir()
        (workspace / "templates").mkdir()
        (workspace / "manifests").mkdir()

        site_file = workspace / "sites" / "test-site.yaml"
        site_file.write_text(
            """
apiVersion: siteops/v1
kind: Site
name: test-site
subscription: "00000000-0000-0000-0000-000000000000"
resourceGroup: rg-test
location: eastus
"""
        )

        params_file = workspace / "parameters" / "invalid.yaml"
        params_file.write_text(
            """
location: eastus
  invalid indentation: broken
    this: is not valid yaml
"""
        )

        template_file = workspace / "templates" / "test.bicep"
        template_file.write_text("param location string")

        manifest_file = workspace / "manifests" / "test.yaml"
        manifest_file.write_text(
            """
apiVersion: siteops/v1
kind: Manifest
name: test
description: Test
sites:
  - test-site
parameters:
  - parameters/invalid.yaml
steps:
  - name: test-step
    template: templates/test.bicep
    scope: resourceGroup
"""
        )

        orchestrator = Orchestrator(workspace)
        errors = orchestrator.validate(manifest_file)

        assert any("Invalid manifest parameter file" in e for e in errors)
        assert any("invalid.yaml" in e for e in errors)

    def test_validate_multiple_manifest_parameters(self, tmp_path):
        """Test validation with multiple manifest parameter files (one missing)."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "sites").mkdir()
        (workspace / "parameters").mkdir()
        (workspace / "templates").mkdir()
        (workspace / "manifests").mkdir()

        site_file = workspace / "sites" / "test-site.yaml"
        site_file.write_text(
            """
apiVersion: siteops/v1
kind: Site
name: test-site
subscription: "00000000-0000-0000-0000-000000000000"
resourceGroup: rg-test
location: eastus
"""
        )

        params_file1 = workspace / "parameters" / "common.yaml"
        params_file1.write_text("location: eastus")

        template_file = workspace / "templates" / "test.bicep"
        template_file.write_text("param location string")

        manifest_file = workspace / "manifests" / "test.yaml"
        manifest_file.write_text(
            """
apiVersion: siteops/v1
kind: Manifest
name: test
description: Test
sites:
  - test-site
parameters:
  - parameters/common.yaml
  - parameters/missing.yaml
steps:
  - name: test-step
    template: templates/test.bicep
    scope: resourceGroup
"""
        )

        orchestrator = Orchestrator(workspace)
        errors = orchestrator.validate(manifest_file)

        assert any("Manifest parameter file not found" in e for e in errors)
        assert any("missing.yaml" in e for e in errors)
        assert not any("common.yaml" in e for e in errors)

    def test_validate_manifest_parameters_empty_list(self, tmp_path):
        """Test that empty manifest parameters list passes validation."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "sites").mkdir()
        (workspace / "templates").mkdir()
        (workspace / "manifests").mkdir()

        site_file = workspace / "sites" / "test-site.yaml"
        site_file.write_text(
            """
apiVersion: siteops/v1
kind: Site
name: test-site
subscription: "00000000-0000-0000-0000-000000000000"
resourceGroup: rg-test
location: eastus
"""
        )

        template_file = workspace / "templates" / "test.bicep"
        template_file.write_text("param location string")

        manifest_file = workspace / "manifests" / "test.yaml"
        manifest_file.write_text(
            """
apiVersion: siteops/v1
kind: Manifest
name: test
description: Test
sites:
  - test-site
parameters: []
steps:
  - name: test-step
    template: templates/test.bicep
    scope: resourceGroup
"""
        )

        orchestrator = Orchestrator(workspace)
        errors = orchestrator.validate(manifest_file)

        param_errors = [e for e in errors if "Manifest parameter" in e]
        assert param_errors == []

    def test_validate_manifest_parameters_no_field(self, tmp_path):
        """Test that missing parameters field passes validation."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "sites").mkdir()
        (workspace / "templates").mkdir()
        (workspace / "manifests").mkdir()

        site_file = workspace / "sites" / "test-site.yaml"
        site_file.write_text(
            """
apiVersion: siteops/v1
kind: Site
name: test-site
subscription: "00000000-0000-0000-0000-000000000000"
resourceGroup: rg-test
location: eastus
"""
        )

        template_file = workspace / "templates" / "test.bicep"
        template_file.write_text("param location string")

        manifest_file = workspace / "manifests" / "test.yaml"
        manifest_file.write_text(
            """
apiVersion: siteops/v1
kind: Manifest
name: test
description: Test
sites:
  - test-site
steps:
  - name: test-step
    template: templates/test.bicep
    scope: resourceGroup
"""
        )

        orchestrator = Orchestrator(workspace)
        errors = orchestrator.validate(manifest_file)

        param_errors = [e for e in errors if "Manifest parameter" in e]
        assert param_errors == []

    def test_validate_truthy_condition_syntax(self, complete_workspace):
        """Test that truthy condition syntax passes validation."""
        orchestrator = Orchestrator(complete_workspace)

        manifest_data = {
            "name": "truthy-condition",
            "sites": ["test-site"],
            "steps": [
                {
                    "name": "step1",
                    "template": "templates/test.bicep",
                    "when": "{{ site.properties.deployOptions.enabled }}",
                }
            ],
        }
        manifest_path = complete_workspace / "manifests" / "truthy.yaml"
        with open(manifest_path, "w", encoding="utf-8") as f:
            yaml.dump(manifest_data, f)

        errors = orchestrator.validate(manifest_path)
        condition_errors = [e for e in errors if "condition" in e.lower() or "when" in e.lower()]
        assert condition_errors == [], f"Unexpected condition errors: {condition_errors}"

    def test_validate_unquoted_boolean_condition_syntax(self, complete_workspace):
        """Test that unquoted boolean condition syntax passes validation."""
        orchestrator = Orchestrator(complete_workspace)

        manifest_data = {
            "name": "boolean-condition",
            "sites": ["test-site"],
            "steps": [
                {
                    "name": "step1",
                    "template": "templates/test.bicep",
                    "when": "{{ site.properties.includeSolution == true }}",
                }
            ],
        }
        manifest_path = complete_workspace / "manifests" / "boolean.yaml"
        with open(manifest_path, "w", encoding="utf-8") as f:
            yaml.dump(manifest_data, f)

        errors = orchestrator.validate(manifest_path)
        condition_errors = [e for e in errors if "condition" in e.lower() or "when" in e.lower()]
        assert condition_errors == [], f"Unexpected condition errors: {condition_errors}"


class TestStepOutputReferenceValidation:
    """Tests for {{ steps.X.outputs.Y }} reference validation."""

    def _create_test_workspace(self, tmp_workspace, manifest_yaml, param_files):
        """Helper to create workspace with manifest and parameter files."""
        (tmp_workspace / "manifests").mkdir(exist_ok=True)
        (tmp_workspace / "templates").mkdir(exist_ok=True)
        (tmp_workspace / "parameters").mkdir(exist_ok=True)
        (tmp_workspace / "sites").mkdir(exist_ok=True)

        (tmp_workspace / "manifests" / "test.yaml").write_text(manifest_yaml)
        (tmp_workspace / "templates" / "test.bicep").write_text("// bicep template")
        (tmp_workspace / "sites" / "test-site.yaml").write_text(
            "name: test-site\n"
            "subscription: '00000000-0000-0000-0000-000000000000'\n"
            "resourceGroup: rg-test\n"
            "location: eastus\n"
        )

        for path, content in param_files.items():
            param_path = tmp_workspace / path
            param_path.parent.mkdir(parents=True, exist_ok=True)
            param_path.write_text(content)

        return tmp_workspace / "manifests" / "test.yaml"

    def test_valid_reference_to_prior_step(self, tmp_workspace):
        """Reference to a step that runs earlier should pass validation."""
        manifest = """
apiVersion: siteops/v1
kind: Manifest
name: test
sites: [test-site]
steps:
  - name: step1
    template: templates/test.bicep
    parameters: [parameters/step1.yaml]
  - name: step2
    template: templates/test.bicep
    parameters: [parameters/step2.yaml]
"""
        manifest_path = self._create_test_workspace(
            tmp_workspace,
            manifest,
            {
                "parameters/step1.yaml": "param1: value1",
                "parameters/step2.yaml": 'resourceId: "{{ steps.step1.outputs.id }}"',
            },
        )

        orchestrator = Orchestrator(tmp_workspace)
        errors = orchestrator.validate(manifest_path)

        assert not errors, f"Expected no errors, got: {errors}"

    def test_reference_to_nonexistent_step(self, tmp_workspace):
        """Reference to a step that doesn't exist should fail."""
        manifest = """
apiVersion: siteops/v1
kind: Manifest
name: test
sites: [test-site]
steps:
  - name: step1
    template: templates/test.bicep
    parameters: [parameters/step1.yaml]
"""
        manifest_path = self._create_test_workspace(
            tmp_workspace,
            manifest,
            {"parameters/step1.yaml": 'value: "{{ steps.nonexistent.outputs.id }}"'},
        )

        orchestrator = Orchestrator(tmp_workspace)
        errors = orchestrator.validate(manifest_path)

        assert len(errors) == 1
        assert "unknown step 'nonexistent'" in errors[0]
        assert "step1" in errors[0]

    def test_reference_to_later_step(self, tmp_workspace):
        """Reference to a step that runs later should fail."""
        manifest = """
apiVersion: siteops/v1
kind: Manifest
name: test
sites: [test-site]
steps:
  - name: first
    template: templates/test.bicep
    parameters: [parameters/first.yaml]
  - name: second
    template: templates/test.bicep
    parameters: [parameters/second.yaml]
"""
        manifest_path = self._create_test_workspace(
            tmp_workspace,
            manifest,
            {
                "parameters/first.yaml": 'value: "{{ steps.second.outputs.id }}"',
                "parameters/second.yaml": "param: value",
            },
        )

        orchestrator = Orchestrator(tmp_workspace)
        errors = orchestrator.validate(manifest_path)

        assert len(errors) == 1
        assert "runs later" in errors[0]

    def test_nested_references_in_dict(self, tmp_workspace):
        """References nested in dict structures should be validated."""
        manifest = """
apiVersion: siteops/v1
kind: Manifest
name: test
sites: [test-site]
steps:
  - name: step1
    template: templates/test.bicep
    scope: resourceGroup
    parameters: [parameters/step1.yaml]
"""
        manifest_path = self._create_test_workspace(
            tmp_workspace,
            manifest,
            {
                "parameters/step1.yaml": """
config:
  nested:
    deep:
      value: "{{ steps.unknown.outputs.id }}"
""",
            },
        )

        orchestrator = Orchestrator(tmp_workspace)
        errors = orchestrator.validate(manifest_path)

        assert len(errors) == 1
        assert "unknown step 'unknown'" in errors[0]

    def test_references_in_list(self, tmp_workspace):
        """References in list items should be validated."""
        manifest = """
apiVersion: siteops/v1
kind: Manifest
name: test
sites: [test-site]
steps:
  - name: step1
    template: templates/test.bicep
    scope: resourceGroup
    parameters: [parameters/step1.yaml]
"""
        manifest_path = self._create_test_workspace(
            tmp_workspace,
            manifest,
            {
                "parameters/step1.yaml": """
items:
  - "{{ steps.missing1.outputs.a }}"
  - static-value
  - "{{ steps.missing2.outputs.b }}"
""",
            },
        )

        orchestrator = Orchestrator(tmp_workspace)
        errors = orchestrator.validate(manifest_path)

        assert len(errors) == 2
        assert any("'missing1'" in e for e in errors)
        assert any("'missing2'" in e for e in errors)

    def test_multiple_references_in_single_string(self, tmp_workspace):
        """Multiple references in one string should all be validated."""
        manifest = """
apiVersion: siteops/v1
kind: Manifest
name: test
sites: [test-site]
steps:
  - name: step1
    template: templates/test.bicep
    scope: resourceGroup
    parameters: [parameters/step1.yaml]
  - name: step2
    template: templates/test.bicep
    scope: resourceGroup
    parameters: [parameters/step2.yaml]
"""
        manifest_path = self._create_test_workspace(
            tmp_workspace,
            manifest,
            {
                "parameters/step1.yaml": "param: value",
                # step2 refs step1 (valid) and unknown (invalid) in same string
                "parameters/step2.yaml": 'combined: "{{ steps.step1.outputs.a }}-{{ steps.unknown.outputs.b }}"',
            },
        )

        orchestrator = Orchestrator(tmp_workspace)
        errors = orchestrator.validate(manifest_path)

        assert len(errors) == 1
        assert "unknown step 'unknown'" in errors[0]

    def test_valid_chain_of_three_steps(self, tmp_workspace):
        """Chain of valid references across multiple steps should pass."""
        manifest = """
apiVersion: siteops/v1
kind: Manifest
name: test
sites: [test-site]
steps:
  - name: create-storage
    template: templates/test.bicep
    scope: resourceGroup
    parameters: [parameters/storage.yaml]
  - name: create-registry
    template: templates/test.bicep
    scope: resourceGroup
    parameters: [parameters/registry.yaml]
  - name: create-instance
    template: templates/test.bicep
    scope: resourceGroup
    parameters: [parameters/instance.yaml]
"""
        manifest_path = self._create_test_workspace(
            tmp_workspace,
            manifest,
            {
                "parameters/storage.yaml": "name: storage",
                "parameters/registry.yaml": 'storageId: "{{ steps.create-storage.outputs.id }}"',
                "parameters/instance.yaml": """
storageId: "{{ steps.create-storage.outputs.id }}"
registryId: "{{ steps.create-registry.outputs.id }}"
""",
            },
        )

        orchestrator = Orchestrator(tmp_workspace)
        errors = orchestrator.validate(manifest_path)

        assert not errors, f"Expected no errors, got: {errors}"

    def test_no_references_passes(self, tmp_workspace):
        """Parameters without step references should pass."""
        manifest = """
apiVersion: siteops/v1
kind: Manifest
name: test
sites: [test-site]
steps:
  - name: step1
    template: templates/test.bicep
    scope: resourceGroup
    parameters: [parameters/step1.yaml]
"""
        manifest_path = self._create_test_workspace(
            tmp_workspace,
            manifest,
            {
                "parameters/step1.yaml": """
simpleValue: hello
nestedConfig:
  key: value
  list: [a, b, c]
""",
            },
        )

        orchestrator = Orchestrator(tmp_workspace)
        errors = orchestrator.validate(manifest_path)

        assert not errors

    def test_site_variable_references_ignored(self, tmp_workspace):
        """{{ site.X }} references should not be flagged as step reference errors."""
        manifest = """
apiVersion: siteops/v1
kind: Manifest
name: test
sites: [test-site]
steps:
  - name: step1
    template: templates/test.bicep
    scope: resourceGroup
    parameters: [parameters/step1.yaml]
"""
        manifest_path = self._create_test_workspace(
            tmp_workspace,
            manifest,
            {
                "parameters/step1.yaml": """
location: "{{ site.location }}"
name: "{{ site.name }}"
cluster: "{{ site.labels.clusterName }}"
""",
            },
        )

        orchestrator = Orchestrator(tmp_workspace)
        errors = orchestrator.validate(manifest_path)

        assert not errors

    def test_self_reference_allowed_when_filtered(self, tmp_workspace):
        """Self-references should be allowed if auto-filtering will remove them."""
        template = tmp_workspace / "templates" / "simple.bicep"
        template.parent.mkdir(parents=True, exist_ok=True)
        template.write_text("param location string\nparam name string\n")

        params = tmp_workspace / "parameters" / "chaining.yaml"
        params.parent.mkdir(parents=True, exist_ok=True)
        params.write_text('location: eastus\nname: my-resource\nfilteredParam: "{{ steps.my-step.outputs.id }}"\n')

        manifest = tmp_workspace / "manifests" / "test.yaml"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(
            """
apiVersion: siteops/v1
kind: Manifest
name: test
sites: [test-site]
steps:
  - name: my-step
    template: templates/simple.bicep
    parameters: [parameters/chaining.yaml]
"""
        )

        (tmp_workspace / "sites" / "test-site.yaml").write_text(
            """
apiVersion: siteops/v1
kind: Site
name: test-site
subscription: "00000000-0000-0000-0000-000000000000"
resourceGroup: rg-test
location: eastus
"""
        )

        from siteops.executor import get_template_parameters

        get_template_parameters.cache_clear()

        orchestrator = Orchestrator(tmp_workspace)
        errors = orchestrator.validate(manifest)

        self_ref_errors = [e for e in errors if "cannot reference its own outputs" in e]
        assert not self_ref_errors, f"Unexpected self-reference errors: {self_ref_errors}"

    def test_self_reference_error_when_template_accepts_param(self, tmp_workspace):
        """Self-references should error if template accepts the parameter."""
        # Template that DOES accept instanceName
        template = tmp_workspace / "templates" / "instance.bicep"
        template.parent.mkdir(parents=True, exist_ok=True)
        template.write_text("param clusterName string\nparam instanceName string\n")

        # Parameter file with self-reference to a param the template accepts
        params = tmp_workspace / "parameters" / "bad-chaining.yaml"
        params.parent.mkdir(parents=True, exist_ok=True)
        params.write_text('clusterName: my-cluster\ninstanceName: "{{ steps.my-step.outputs.name }}"\n')

        manifest = tmp_workspace / "manifests" / "test.yaml"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(
            """
apiVersion: siteops/v1
kind: Manifest
name: test
sites:
  - test-site
steps:
  - name: my-step
    template: templates/instance.bicep
    parameters:
      - parameters/bad-chaining.yaml
"""
        )

        (tmp_workspace / "sites" / "test-site.yaml").write_text(
            """
apiVersion: siteops/v1
kind: Site
name: test-site
subscription: "00000000-0000-0000-0000-000000000000"
resourceGroup: rg-test
location: eastus
"""
        )

        from siteops.executor import get_template_parameters

        get_template_parameters.cache_clear()

        orchestrator = Orchestrator(tmp_workspace)
        errors = orchestrator.validate(manifest)

        # SHOULD error - template accepts instanceName, so self-ref is invalid
        self_ref_errors = [e for e in errors if "cannot reference its own outputs" in e]
        assert self_ref_errors, f"Expected self-reference error, got: {errors}"

    def test_self_reference_conservative_when_template_unreadable(self, tmp_workspace):
        """Self-references should error if template params can't be extracted."""
        # Create template that will fail to parse
        template = tmp_workspace / "templates" / "bad.bicep"
        template.parent.mkdir(parents=True, exist_ok=True)
        template.write_text("param location string")

        params = tmp_workspace / "parameters" / "chaining.yaml"
        params.parent.mkdir(parents=True, exist_ok=True)
        params.write_text('location: eastus\nselfRef: "{{ steps.my-step.outputs.id }}"\n')

        manifest = tmp_workspace / "manifests" / "test.yaml"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(
            """
apiVersion: siteops/v1
kind: Manifest
name: test
sites:
  - test-site
steps:
  - name: my-step
    template: templates/bad.bicep
    parameters:
      - parameters/chaining.yaml
"""
        )

        (tmp_workspace / "sites" / "test-site.yaml").write_text(
            """
apiVersion: siteops/v1
kind: Site
name: test-site
subscription: "00000000-0000-0000-0000-000000000000"
resourceGroup: rg-test
location: eastus
"""
        )

        from siteops.executor import get_template_parameters

        get_template_parameters.cache_clear()

        orchestrator = Orchestrator(tmp_workspace)

        # Mock get_template_parameters to simulate extraction failure
        with patch("siteops.executor.get_template_parameters", side_effect=ValueError("Mock failure")):
            errors = orchestrator.validate(manifest)

        # SHOULD error - can't verify auto-filtering, be conservative
        self_ref_errors = [e for e in errors if "cannot reference its own outputs" in e]
        assert self_ref_errors, f"Expected conservative self-reference error, got: {errors}"

    def test_shared_chaining_file_with_multiple_steps(self, tmp_workspace):
        """A shared chaining.yaml should work when self-refs are auto-filtered."""
        # Template for aio-instance (does NOT accept aioInstanceName - it generates it)
        aio_template = tmp_workspace / "templates" / "aio-instance.bicep"
        aio_template.parent.mkdir(parents=True, exist_ok=True)
        aio_template.write_text("param clusterName string\nparam schemaRegistryId string\n")

        # Template for quickstart (DOES accept aioInstanceName)
        quickstart_template = tmp_workspace / "templates" / "quickstart.bicep"
        quickstart_template.write_text("param aioInstanceName string\nparam clusterName string\n")

        # Shared chaining file with outputs from various steps
        chaining = tmp_workspace / "parameters" / "chaining.yaml"
        chaining.parent.mkdir(parents=True, exist_ok=True)
        chaining.write_text(
            """
# Outputs for aio-instance step
schemaRegistryId: "{{ steps.schema-registry.outputs.id }}"

# Outputs from aio-instance (used by later steps)
aioInstanceName: "{{ steps.aio-instance.outputs.name }}"
"""
        )

        manifest = tmp_workspace / "manifests" / "test.yaml"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(
            """
apiVersion: siteops/v1
kind: Manifest
name: test
sites:
  - test-site
steps:
  - name: schema-registry
    template: templates/aio-instance.bicep
  - name: aio-instance
    template: templates/aio-instance.bicep
    parameters:
      - parameters/chaining.yaml
  - name: quickstart
    template: templates/quickstart.bicep
    parameters:
      - parameters/chaining.yaml
"""
        )

        (tmp_workspace / "sites" / "test-site.yaml").write_text(
            """
apiVersion: siteops/v1
kind: Site
name: test-site
subscription: "00000000-0000-0000-0000-000000000000"
resourceGroup: rg-test
location: eastus
"""
        )

        from siteops.executor import get_template_parameters

        get_template_parameters.cache_clear()

        orchestrator = Orchestrator(tmp_workspace)
        errors = orchestrator.validate(manifest)

        # aio-instance step:
        #   - schemaRegistryId refs schema-registry (valid - prior step)
        #   - aioInstanceName refs self BUT template doesn't accept it (filtered)
        # quickstart step:
        #   - aioInstanceName refs aio-instance (valid - prior step)
        #   - schemaRegistryId refs schema-registry (valid - prior step)
        assert not errors, f"Expected no errors, got: {errors}"

class TestSubscriptionScopedValidation:
    """Tests for subscription-scoped step validation."""

    def test_subscription_step_without_subscription_site(self, tmp_workspace, sample_bicep_template):
        """Error when subscription-scoped step has no subscription-level site."""
        # Create RG-level site only
        (tmp_workspace / "sites" / "rg-site.yaml").write_text(
            """
apiVersion: siteops/v1
kind: Site
name: rg-site
subscription: "00000000-0000-0000-0000-000000000001"
resourceGroup: rg-test
location: eastus
"""
        )

        # Create manifest with subscription-scoped step
        manifest_path = tmp_workspace / "manifests" / "sub-scoped.yaml"
        manifest_path.write_text(
            """
name: sub-scoped
sites:
  - rg-site
steps:
  - name: shared-resource
    template: templates/test.bicep
    scope: subscription
"""
        )

        orchestrator = Orchestrator(tmp_workspace)
        errors = orchestrator.validate(manifest_path)

        assert any("subscription-level site" in e for e in errors)
        assert any("subscription-scoped steps" in e for e in errors)

    def test_subscription_step_with_subscription_site(self, tmp_workspace, sample_bicep_template):
        """No error when subscription-scoped step has subscription-level site."""
        # Create subscription-level site (no resourceGroup)
        (tmp_workspace / "sites" / "sub-site.yaml").write_text(
            """
apiVersion: siteops/v1
kind: Site
name: sub-site
subscription: "00000000-0000-0000-0000-000000000001"
location: eastus
"""
        )

        # Create manifest with subscription-scoped step
        manifest_path = tmp_workspace / "manifests" / "sub-scoped.yaml"
        manifest_path.write_text(
            """
name: sub-scoped
sites:
  - sub-site
steps:
  - name: shared-resource
    template: templates/test.bicep
    scope: subscription
"""
        )

        orchestrator = Orchestrator(tmp_workspace)
        errors = orchestrator.validate(manifest_path)

        # Should not have subscription-level site errors
        assert not any("subscription-level site" in e for e in errors)

    def test_multiple_subscription_sites_same_subscription(self, tmp_workspace, sample_bicep_template):
        """Error when multiple subscription-level sites exist for same subscription."""
        sub_id = "00000000-0000-0000-0000-000000000001"

        # Create two subscription-level sites with same subscription
        (tmp_workspace / "sites" / "sub-site-1.yaml").write_text(
            f"""
apiVersion: siteops/v1
kind: Site
name: sub-site-1
subscription: "{sub_id}"
location: eastus
"""
        )

        (tmp_workspace / "sites" / "sub-site-2.yaml").write_text(
            f"""
apiVersion: siteops/v1
kind: Site
name: sub-site-2
subscription: "{sub_id}"
location: westus
"""
        )

        # Create manifest with subscription-scoped step
        manifest_path = tmp_workspace / "manifests" / "sub-scoped.yaml"
        manifest_path.write_text(
            """
name: sub-scoped
sites:
  - sub-site-1
  - sub-site-2
steps:
  - name: shared-resource
    template: templates/test.bicep
    scope: subscription
"""
        )

        orchestrator = Orchestrator(tmp_workspace)
        errors = orchestrator.validate(manifest_path)

        assert any("multiple subscription-level sites" in e.lower() for e in errors)

    def test_mixed_sites_valid_hierarchy(self, tmp_workspace, sample_bicep_template):
        """Valid when subscription-level and RG-level sites exist for same subscription."""
        sub_id = "00000000-0000-0000-0000-000000000001"

        # Create subscription-level site
        (tmp_workspace / "sites" / "sub-site.yaml").write_text(
            f"""
apiVersion: siteops/v1
kind: Site
name: sub-site
subscription: "{sub_id}"
location: eastus
"""
        )

        # Create RG-level site with same subscription
        (tmp_workspace / "sites" / "rg-site.yaml").write_text(
            f"""
apiVersion: siteops/v1
kind: Site
name: rg-site
subscription: "{sub_id}"
resourceGroup: rg-test
location: eastus
"""
        )

        # Create manifest with both subscription and RG-scoped steps
        manifest_path = tmp_workspace / "manifests" / "mixed.yaml"
        manifest_path.write_text(
            """
name: mixed
sites:
  - sub-site
  - rg-site
steps:
  - name: sub-step
    template: templates/test.bicep
    scope: subscription
  - name: rg-step
    template: templates/test.bicep
    scope: resourceGroup
"""
        )

        orchestrator = Orchestrator(tmp_workspace)
        errors = orchestrator.validate(manifest_path)

        # Should not have subscription-level site errors
        assert not any("subscription-level site" in e for e in errors)
        assert not any("multiple subscription-level sites" in e.lower() for e in errors)

    def test_no_subscription_step_validation_skipped(self, tmp_workspace, sample_bicep_template):
        """No validation errors when manifest has no subscription-scoped steps."""
        # Create RG-level site only
        (tmp_workspace / "sites" / "rg-site.yaml").write_text(
            """
apiVersion: siteops/v1
kind: Site
name: rg-site
subscription: "00000000-0000-0000-0000-000000000001"
resourceGroup: rg-test
location: eastus
"""
        )

        # Create manifest with only RG-scoped steps
        manifest_path = tmp_workspace / "manifests" / "rg-only.yaml"
        manifest_path.write_text(
            """
name: rg-only
sites:
  - rg-site
steps:
  - name: rg-step
    template: templates/test.bicep
    scope: resourceGroup
"""
        )

        orchestrator = Orchestrator(tmp_workspace)
        errors = orchestrator.validate(manifest_path)

        # Should have no subscription-related errors
        assert not any("subscription" in e.lower() for e in errors)