"""Tests for parameter resolution and template variable substitution.

Covers:
- Site variable resolution ({{ site.X }})
- Step output chaining ({{ steps.X.outputs.Y }})
- Properties resolution ({{ site.properties.X }})
- Condition evaluation
- Manifest-level parameter merging
"""

import json
import logging

from siteops.models import Manifest, Site
from siteops.orchestrator import Orchestrator


class TestTemplateResolution:
    """Tests for template variable substitution."""

    def test_resolve_site_variables(self, complete_workspace):
        orchestrator = Orchestrator(complete_workspace)
        site = Site(
            name="my-site",
            subscription="sub-123",
            resource_group="rg-test",
            location="westus",
            labels={"env": "prod"},
        )

        value = "Resource in {{ site.location }} for {{ site.labels.env }}"
        result = orchestrator._resolve_template_strings(value, site)

        assert result == "Resource in westus for prod"

    def test_resolve_nested_dict(self, complete_workspace):
        orchestrator = Orchestrator(complete_workspace)
        site = Site(
            name="test",
            subscription="sub",
            resource_group="rg",
            location="eastus",
            labels={},
        )

        value = {
            "location": "{{ site.location }}",
            "tags": {"site": "{{ site.name }}"},
        }
        result = orchestrator._resolve_template_strings(value, site)

        assert result["location"] == "eastus"
        assert result["tags"]["site"] == "test"

    def test_resolve_list(self, complete_workspace):
        orchestrator = Orchestrator(complete_workspace)
        site = Site(
            name="test",
            subscription="sub",
            resource_group="rg",
            location="eastus",
            labels={},
        )

        value = ["{{ site.name }}", "static", "{{ site.location }}"]
        result = orchestrator._resolve_template_strings(value, site)

        assert result == ["test", "static", "eastus"]


class TestStepOutputChaining:
    """Tests for {{ steps.X.outputs.Y }} resolution."""

    def test_resolve_step_output_simple(self, complete_workspace):
        orchestrator = Orchestrator(complete_workspace)
        step_outputs = {"deploy-storage": {"storageId": "storage-123"}}

        value = "{{ steps.deploy-storage.outputs.storageId }}"
        result = orchestrator._resolve_step_outputs(value, step_outputs)

        assert result == "storage-123"

    def test_resolve_step_output_nested(self, complete_workspace):
        orchestrator = Orchestrator(complete_workspace)
        step_outputs = {
            "deploy-network": {
                "vnet": {"value": {"id": "vnet-123"}, "type": "Object"},
            },
        }

        value = "{{ steps.deploy-network.outputs.vnet.id }}"
        result = orchestrator._resolve_step_outputs(value, step_outputs)

        assert result == "vnet-123"

    def test_resolve_step_output_in_string(self, complete_workspace):
        orchestrator = Orchestrator(complete_workspace)
        step_outputs = {"step1": {"name": "myresource"}}

        value = "Resource: {{ steps.step1.outputs.name }} is ready"
        result = orchestrator._resolve_step_outputs(value, step_outputs)

        assert result == "Resource: myresource is ready"

    def test_resolve_step_output_missing(self, complete_workspace):
        orchestrator = Orchestrator(complete_workspace)
        step_outputs = {}

        value = "{{ steps.missing.outputs.value }}"
        result = orchestrator._resolve_step_outputs(value, step_outputs)

        assert result == value

    def test_resolve_complex_output_type(self, complete_workspace):
        """When entire value is a template, return the actual type (list/dict)."""
        orchestrator = Orchestrator(complete_workspace)
        step_outputs = {"step1": {"ids": ["id-1", "id-2", "id-3"]}}

        value = "{{ steps.step1.outputs.ids }}"
        result = orchestrator._resolve_step_outputs(value, step_outputs)

        assert result == ["id-1", "id-2", "id-3"]


class TestConditionEvaluation:
    """Tests for when condition evaluation."""

    def test_no_condition(self, complete_workspace):
        orchestrator = Orchestrator(complete_workspace)
        site = Site(name="test", subscription="sub", resource_group="rg", location="eastus")

        assert orchestrator._evaluate_condition(None, site) is True
        assert orchestrator._evaluate_condition("", site) is True

    def test_equals_condition_match(self, complete_workspace):
        orchestrator = Orchestrator(complete_workspace)
        site = Site(
            name="test",
            subscription="sub",
            resource_group="rg",
            location="eastus",
            labels={"env": "prod"},
        )

        result = orchestrator._evaluate_condition("{{ site.labels.env == 'prod' }}", site)
        assert result is True

    def test_equals_condition_no_match(self, complete_workspace):
        orchestrator = Orchestrator(complete_workspace)
        site = Site(
            name="test",
            subscription="sub",
            resource_group="rg",
            location="eastus",
            labels={"env": "dev"},
        )

        result = orchestrator._evaluate_condition("{{ site.labels.env == 'prod' }}", site)
        assert result is False

    def test_not_equals_condition(self, complete_workspace):
        orchestrator = Orchestrator(complete_workspace)
        site = Site(
            name="test",
            subscription="sub",
            resource_group="rg",
            location="eastus",
            labels={"env": "dev"},
        )

        result = orchestrator._evaluate_condition("{{ site.labels.env != 'prod' }}", site)
        assert result is True

    def test_missing_label_treated_as_empty(self, complete_workspace):
        orchestrator = Orchestrator(complete_workspace)
        site = Site(
            name="test",
            subscription="sub",
            resource_group="rg",
            location="eastus",
            labels={},
        )

        result = orchestrator._evaluate_condition("{{ site.labels.env == '' }}", site)
        assert result is True

    def test_properties_condition_equals_true(self, complete_workspace):
        """Test {{ site.properties.path == true }} with boolean true."""
        orchestrator = Orchestrator(complete_workspace)
        site = Site(
            name="test",
            subscription="sub",
            resource_group="rg",
            location="eastus",
            properties={"deployOptions": {"includeSolution": True}},
        )

        result = orchestrator._evaluate_condition("{{ site.properties.deployOptions.includeSolution == true }}", site)
        assert result is True

    def test_properties_condition_equals_false(self, complete_workspace):
        """Test {{ site.properties.path == false }} with boolean false."""
        orchestrator = Orchestrator(complete_workspace)
        site = Site(
            name="test",
            subscription="sub",
            resource_group="rg",
            location="eastus",
            properties={"deployOptions": {"includeSolution": False}},
        )

        result = orchestrator._evaluate_condition("{{ site.properties.deployOptions.includeSolution == false }}", site)
        assert result is True

    def test_properties_condition_not_equals(self, complete_workspace):
        """Test {{ site.properties.path != 'value' }}."""
        orchestrator = Orchestrator(complete_workspace)
        site = Site(
            name="test",
            subscription="sub",
            resource_group="rg",
            location="eastus",
            properties={"tier": "standard"},
        )

        result = orchestrator._evaluate_condition("{{ site.properties.tier != 'premium' }}", site)
        assert result is True

    def test_properties_condition_nested_path(self, complete_workspace):
        """Test {{ site.properties.deep.nested.path == 'value' }}."""
        orchestrator = Orchestrator(complete_workspace)
        site = Site(
            name="test",
            subscription="sub",
            resource_group="rg",
            location="eastus",
            properties={"deep": {"nested": {"path": "expected"}}},
        )

        result = orchestrator._evaluate_condition("{{ site.properties.deep.nested.path == 'expected' }}", site)
        assert result is True

    def test_properties_condition_missing_path(self, complete_workspace):
        """Test condition with missing property path returns False for == comparisons."""
        orchestrator = Orchestrator(complete_workspace)
        site = Site(
            name="test",
            subscription="sub",
            resource_group="rg",
            location="eastus",
            properties={},
        )

        # Missing property compared to 'true' should not match (actual_value is "")
        result = orchestrator._evaluate_condition("{{ site.properties.nonexistent == true }}", site)
        assert result is False

    def test_properties_condition_quoted_string(self, complete_workspace):
        """Test {{ site.properties.path == 'string-value' }}."""
        orchestrator = Orchestrator(complete_workspace)
        site = Site(
            name="test",
            subscription="sub",
            resource_group="rg",
            location="eastus",
            properties={"environment": "production"},
        )

        result = orchestrator._evaluate_condition("{{ site.properties.environment == 'production' }}", site)
        assert result is True

    def test_properties_condition_double_quotes(self, complete_workspace):
        """Test {{ site.properties.path == "value" }} with double quotes."""
        orchestrator = Orchestrator(complete_workspace)
        site = Site(
            name="test",
            subscription="sub",
            resource_group="rg",
            location="eastus",
            properties={"name": "my-resource"},
        )

        result = orchestrator._evaluate_condition('{{ site.properties.name == "my-resource" }}', site)
        assert result is True


class TestTruthyConditionEvaluation:
    """Tests for truthy condition evaluation (no comparison operator)."""

    def test_truthy_boolean_true(self, complete_workspace):
        """Test {{ site.properties.path }} with boolean True."""
        orchestrator = Orchestrator(complete_workspace)
        site = Site(
            name="test",
            subscription="sub",
            resource_group="rg",
            location="eastus",
            properties={"enabled": True},
        )

        result = orchestrator._evaluate_condition("{{ site.properties.enabled }}", site)
        assert result is True

    def test_truthy_boolean_false(self, complete_workspace):
        """Test {{ site.properties.path }} with boolean False."""
        orchestrator = Orchestrator(complete_workspace)
        site = Site(
            name="test",
            subscription="sub",
            resource_group="rg",
            location="eastus",
            properties={"enabled": False},
        )

        result = orchestrator._evaluate_condition("{{ site.properties.enabled }}", site)
        assert result is False

    def test_truthy_nested_boolean(self, complete_workspace):
        """Test {{ site.properties.nested.path }} with nested boolean."""
        orchestrator = Orchestrator(complete_workspace)
        site = Site(
            name="test",
            subscription="sub",
            resource_group="rg",
            location="eastus",
            properties={"deployOptions": {"includeSolution": True}},
        )

        result = orchestrator._evaluate_condition("{{ site.properties.deployOptions.includeSolution }}", site)
        assert result is True

    def test_truthy_string_non_empty(self, complete_workspace):
        """Test truthy check with non-empty string returns True."""
        orchestrator = Orchestrator(complete_workspace)
        site = Site(
            name="test",
            subscription="sub",
            resource_group="rg",
            location="eastus",
            properties={"value": "something"},
        )

        result = orchestrator._evaluate_condition("{{ site.properties.value }}", site)
        assert result is True

    def test_truthy_string_empty(self, complete_workspace):
        """Test truthy check with empty string returns False."""
        orchestrator = Orchestrator(complete_workspace)
        site = Site(
            name="test",
            subscription="sub",
            resource_group="rg",
            location="eastus",
            properties={"value": ""},
        )

        result = orchestrator._evaluate_condition("{{ site.properties.value }}", site)
        assert result is False

    def test_truthy_string_false(self, complete_workspace):
        """Test truthy check with string 'false' returns False."""
        orchestrator = Orchestrator(complete_workspace)
        site = Site(
            name="test",
            subscription="sub",
            resource_group="rg",
            location="eastus",
            properties={"value": "false"},
        )

        result = orchestrator._evaluate_condition("{{ site.properties.value }}", site)
        assert result is False

    def test_truthy_string_false_uppercase(self, complete_workspace):
        """Test truthy check with string 'FALSE' returns False (case-insensitive)."""
        orchestrator = Orchestrator(complete_workspace)
        site = Site(
            name="test",
            subscription="sub",
            resource_group="rg",
            location="eastus",
            properties={"value": "FALSE"},
        )

        result = orchestrator._evaluate_condition("{{ site.properties.value }}", site)
        assert result is False

    def test_truthy_string_zero(self, complete_workspace):
        """Test truthy check with string '0' returns False."""
        orchestrator = Orchestrator(complete_workspace)
        site = Site(
            name="test",
            subscription="sub",
            resource_group="rg",
            location="eastus",
            properties={"value": "0"},
        )

        result = orchestrator._evaluate_condition("{{ site.properties.value }}", site)
        assert result is False

    def test_truthy_number_nonzero(self, complete_workspace):
        """Test truthy check with non-zero number returns True."""
        orchestrator = Orchestrator(complete_workspace)
        site = Site(
            name="test",
            subscription="sub",
            resource_group="rg",
            location="eastus",
            properties={"count": 5},
        )

        result = orchestrator._evaluate_condition("{{ site.properties.count }}", site)
        assert result is True

    def test_truthy_number_zero(self, complete_workspace):
        """Test truthy check with zero returns False."""
        orchestrator = Orchestrator(complete_workspace)
        site = Site(
            name="test",
            subscription="sub",
            resource_group="rg",
            location="eastus",
            properties={"count": 0},
        )

        result = orchestrator._evaluate_condition("{{ site.properties.count }}", site)
        assert result is False

    def test_truthy_list_non_empty(self, complete_workspace):
        """Test truthy check with non-empty list returns True."""
        orchestrator = Orchestrator(complete_workspace)
        site = Site(
            name="test",
            subscription="sub",
            resource_group="rg",
            location="eastus",
            properties={"items": ["a", "b"]},
        )

        result = orchestrator._evaluate_condition("{{ site.properties.items }}", site)
        assert result is True

    def test_truthy_list_empty(self, complete_workspace):
        """Test truthy check with empty list returns False."""
        orchestrator = Orchestrator(complete_workspace)
        site = Site(
            name="test",
            subscription="sub",
            resource_group="rg",
            location="eastus",
            properties={"items": []},
        )

        result = orchestrator._evaluate_condition("{{ site.properties.items }}", site)
        assert result is False

    def test_truthy_dict_non_empty(self, complete_workspace):
        """Test truthy check with non-empty dict returns True."""
        orchestrator = Orchestrator(complete_workspace)
        site = Site(
            name="test",
            subscription="sub",
            resource_group="rg",
            location="eastus",
            properties={"config": {"key": "value"}},
        )

        result = orchestrator._evaluate_condition("{{ site.properties.config }}", site)
        assert result is True

    def test_truthy_dict_empty(self, complete_workspace):
        """Test truthy check with empty dict returns False."""
        orchestrator = Orchestrator(complete_workspace)
        site = Site(
            name="test",
            subscription="sub",
            resource_group="rg",
            location="eastus",
            properties={"config": {}},
        )

        result = orchestrator._evaluate_condition("{{ site.properties.config }}", site)
        assert result is False

    def test_truthy_none_value(self, complete_workspace):
        """Test truthy check with None (missing path) returns False."""
        orchestrator = Orchestrator(complete_workspace)
        site = Site(
            name="test",
            subscription="sub",
            resource_group="rg",
            location="eastus",
            properties={},
        )

        result = orchestrator._evaluate_condition("{{ site.properties.nonexistent }}", site)
        assert result is False

    def test_truthy_with_array_index(self, complete_workspace):
        """Test truthy check with array index path."""
        orchestrator = Orchestrator(complete_workspace)
        site = Site(
            name="test",
            subscription="sub",
            resource_group="rg",
            location="eastus",
            properties={"endpoints": [{"enabled": True}, {"enabled": False}]},
        )

        result = orchestrator._evaluate_condition("{{ site.properties.endpoints[0].enabled }}", site)
        assert result is True

        result = orchestrator._evaluate_condition("{{ site.properties.endpoints[1].enabled }}", site)
        assert result is False

    def test_truthy_float_nonzero(self, complete_workspace):
        """Test truthy check with non-zero float returns True."""
        orchestrator = Orchestrator(complete_workspace)
        site = Site(
            name="test",
            subscription="sub",
            resource_group="rg",
            location="eastus",
            properties={"ratio": 0.5},
        )

        result = orchestrator._evaluate_condition("{{ site.properties.ratio }}", site)
        assert result is True

    def test_truthy_float_zero(self, complete_workspace):
        """Test truthy check with float 0.0 returns False."""
        orchestrator = Orchestrator(complete_workspace)
        site = Site(
            name="test",
            subscription="sub",
            resource_group="rg",
            location="eastus",
            properties={"ratio": 0.0},
        )

        result = orchestrator._evaluate_condition("{{ site.properties.ratio }}", site)
        assert result is False

    def test_truthy_labels_not_supported(self, complete_workspace):
        """Test that truthy check on labels returns True for any non-empty label."""
        orchestrator = Orchestrator(complete_workspace)
        site = Site(
            name="test",
            subscription="sub",
            resource_group="rg",
            location="eastus",
            labels={"enabled": "true"},
        )

        # Labels are always strings, so truthy check treats non-empty strings as True
        result = orchestrator._evaluate_condition("{{ site.labels.enabled }}", site)
        assert result is True

    def test_truthy_labels_empty_string(self, complete_workspace):
        """Test that truthy check on empty label string returns False."""
        orchestrator = Orchestrator(complete_workspace)
        site = Site(
            name="test",
            subscription="sub",
            resource_group="rg",
            location="eastus",
            labels={"flag": ""},
        )

        result = orchestrator._evaluate_condition("{{ site.labels.flag }}", site)
        assert result is False

    def test_truthy_labels_string_false(self, complete_workspace):
        """Test that truthy check on label with string 'false' returns False."""
        orchestrator = Orchestrator(complete_workspace)
        site = Site(
            name="test",
            subscription="sub",
            resource_group="rg",
            location="eastus",
            labels={"enabled": "false"},
        )

        result = orchestrator._evaluate_condition("{{ site.labels.enabled }}", site)
        assert result is False


class TestLabelsTruthyConditionEvaluation:
    """Tests for truthy condition evaluation on labels."""

    def test_truthy_label_non_empty(self, complete_workspace):
        """Test truthy check on non-empty label returns True."""
        orchestrator = Orchestrator(complete_workspace)
        site = Site(
            name="test",
            subscription="sub",
            resource_group="rg",
            location="eastus",
            labels={"enabled": "true"},
        )

        result = orchestrator._evaluate_condition("{{ site.labels.enabled }}", site)
        assert result is True

    def test_truthy_label_empty_string(self, complete_workspace):
        """Test truthy check on empty label returns False."""
        orchestrator = Orchestrator(complete_workspace)
        site = Site(
            name="test",
            subscription="sub",
            resource_group="rg",
            location="eastus",
            labels={"flag": ""},
        )

        result = orchestrator._evaluate_condition("{{ site.labels.flag }}", site)
        assert result is False

    def test_truthy_label_string_false(self, complete_workspace):
        """Test truthy check on label 'false' returns False."""
        orchestrator = Orchestrator(complete_workspace)
        site = Site(
            name="test",
            subscription="sub",
            resource_group="rg",
            location="eastus",
            labels={"enabled": "false"},
        )

        result = orchestrator._evaluate_condition("{{ site.labels.enabled }}", site)
        assert result is False

    def test_truthy_label_string_zero(self, complete_workspace):
        """Test truthy check on label '0' returns False."""
        orchestrator = Orchestrator(complete_workspace)
        site = Site(
            name="test",
            subscription="sub",
            resource_group="rg",
            location="eastus",
            labels={"count": "0"},
        )

        result = orchestrator._evaluate_condition("{{ site.labels.count }}", site)
        assert result is False

    def test_truthy_label_missing(self, complete_workspace):
        """Test truthy check on missing label returns False."""
        orchestrator = Orchestrator(complete_workspace)
        site = Site(
            name="test",
            subscription="sub",
            resource_group="rg",
            location="eastus",
            labels={},
        )

        result = orchestrator._evaluate_condition("{{ site.labels.nonexistent }}", site)
        assert result is False


class TestPropertiesResolution:
    """Tests for site.properties template resolution."""

    def test_resolve_simple_property(self, tmp_workspace):
        orchestrator = Orchestrator(workspace=tmp_workspace)
        site = Site(
            name="test-site",
            subscription="sub-123",
            resource_group="rg-test",
            location="eastus",
            properties={"apiEndpoint": "https://api.example.com"},
        )

        result = orchestrator._resolve_template_strings("{{ site.properties.apiEndpoint }}", site)
        assert result == "https://api.example.com"

    def test_resolve_nested_property(self, tmp_workspace):
        orchestrator = Orchestrator(workspace=tmp_workspace)
        site = Site(
            name="test-site",
            subscription="sub-123",
            resource_group="rg-test",
            location="eastus",
            properties={"mqtt": {"broker": "mqtt://10.0.1.50:1883", "port": 1883}},
        )

        result = orchestrator._resolve_template_strings("{{ site.properties.mqtt.broker }}", site)
        assert result == "mqtt://10.0.1.50:1883"

    def test_resolve_array_index_property(self, tmp_workspace):
        orchestrator = Orchestrator(workspace=tmp_workspace)
        site = Site(
            name="test-site",
            subscription="sub-123",
            resource_group="rg-test",
            location="eastus",
            properties={
                "endpoints": [
                    {"host": "10.0.1.100", "port": 4840},
                    {"host": "10.0.1.101", "port": 4840},
                ]
            },
        )

        result = orchestrator._resolve_template_strings("{{ site.properties.endpoints[0].host }}", site)
        assert result == "10.0.1.100"

    def test_resolve_entire_array_property(self, tmp_workspace):
        orchestrator = Orchestrator(workspace=tmp_workspace)
        site = Site(
            name="test-site",
            subscription="sub-123",
            resource_group="rg-test",
            location="eastus",
            properties={"endpoints": [{"host": "10.0.1.100"}, {"host": "10.0.1.101"}]},
        )

        result = orchestrator._resolve_template_strings("{{ site.properties.endpoints }}", site)
        assert result == [{"host": "10.0.1.100"}, {"host": "10.0.1.101"}]

    def test_resolve_entire_object_property(self, tmp_workspace):
        orchestrator = Orchestrator(workspace=tmp_workspace)
        site = Site(
            name="test-site",
            subscription="sub-123",
            resource_group="rg-test",
            location="eastus",
            properties={"mqtt": {"broker": "mqtt://10.0.1.50:1883", "port": 1883}},
        )

        result = orchestrator._resolve_template_strings("{{ site.properties.mqtt }}", site)
        assert result == {"broker": "mqtt://10.0.1.50:1883", "port": 1883}

    def test_resolve_property_embedded_in_string(self, tmp_workspace):
        orchestrator = Orchestrator(workspace=tmp_workspace)
        site = Site(
            name="test-site",
            subscription="sub-123",
            resource_group="rg-test",
            location="eastus",
            properties={"host": "10.0.1.100", "port": 4840},
        )

        result = orchestrator._resolve_template_strings(
            "opc.tcp://{{ site.properties.host }}:{{ site.properties.port }}", site
        )
        assert result == "opc.tcp://10.0.1.100:4840"

    def test_resolve_missing_property_unchanged(self, tmp_workspace):
        orchestrator = Orchestrator(workspace=tmp_workspace)
        site = Site(
            name="test-site",
            subscription="sub-123",
            resource_group="rg-test",
            location="eastus",
            properties={},
        )

        result = orchestrator._resolve_template_strings("{{ site.properties.nonexistent }}", site)
        assert result == "{{ site.properties.nonexistent }}"


class TestResolveParametersManifestLevel:
    """Tests for manifest-level parameter resolution and filtering."""

    def _setup_workspace(self, tmp_path):
        """Create standard workspace structure."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "parameters").mkdir()
        (workspace / "templates").mkdir()
        (workspace / "sites").mkdir()
        (workspace / "manifests").mkdir()
        return workspace

    def _create_site(self, workspace, content):
        """Create site file."""
        site_file = workspace / "sites" / "test-site.yaml"
        site_file.write_text(content)

    def _create_template(self, workspace, params):
        """Create ARM JSON template with specified parameters."""
        template_file = workspace / "templates" / "test.json"
        template_file.write_text(json.dumps({"parameters": params}))

    def test_manifest_parameters_merged_before_step_parameters(self, tmp_path):
        """Test that manifest parameters are merged before step parameters."""
        workspace = self._setup_workspace(tmp_path)

        self._create_site(
            workspace,
            """
apiVersion: siteops/v1
kind: Site
name: test-site
subscription: "00000000-0000-0000-0000-000000000000"
resourceGroup: rg-test
location: eastus
""",
        )

        (workspace / "parameters" / "common.yaml").write_text(
            "location: westus\nenvironment: shared\nsharedValue: from-manifest\n"
        )
        (workspace / "parameters" / "step.yaml").write_text("environment: step-override\nstepOnlyValue: from-step\n")

        self._create_template(
            workspace,
            {
                "location": {"type": "string"},
                "environment": {"type": "string"},
                "sharedValue": {"type": "string"},
                "stepOnlyValue": {"type": "string"},
            },
        )

        (workspace / "manifests" / "test.yaml").write_text(
            """
apiVersion: siteops/v1
kind: Manifest
name: test
sites: [test-site]
parameters: [parameters/common.yaml]
steps:
  - name: test-step
    template: templates/test.json
    parameters: [parameters/step.yaml]
"""
        )

        from siteops.executor import get_template_parameters

        get_template_parameters.cache_clear()

        orchestrator = Orchestrator(workspace)
        manifest = Manifest.from_file(workspace / "manifests" / "test.yaml")
        site = orchestrator.load_site("test-site")
        step = manifest.steps[0]

        result = orchestrator.resolve_parameters(step, site, manifest, {})

        assert result["environment"] == "step-override"
        assert result["sharedValue"] == "from-manifest"
        assert result["stepOnlyValue"] == "from-step"
        assert result["location"] == "westus"

    def test_manifest_parameters_resolved_with_site_variables(self, tmp_path):
        """Test that {{ site.X }} templates in manifest params are resolved."""
        workspace = self._setup_workspace(tmp_path)

        self._create_site(
            workspace,
            """
apiVersion: siteops/v1
kind: Site
name: test-site
subscription: "00000000-0000-0000-0000-000000000000"
resourceGroup: rg-test
location: eastus
labels:
  environment: dev
  clusterName: arc-dev
""",
        )

        (workspace / "parameters" / "common.yaml").write_text(
            """
location: "{{ site.location }}"
environment: "{{ site.labels.environment }}"
clusterName: "{{ site.labels.clusterName }}"
"""
        )

        self._create_template(
            workspace,
            {
                "location": {"type": "string"},
                "environment": {"type": "string"},
                "clusterName": {"type": "string"},
            },
        )

        (workspace / "manifests" / "test.yaml").write_text(
            """
apiVersion: siteops/v1
kind: Manifest
name: test
sites: [test-site]
parameters: [parameters/common.yaml]
steps:
  - name: test-step
    template: templates/test.json
"""
        )

        from siteops.executor import get_template_parameters

        get_template_parameters.cache_clear()

        orchestrator = Orchestrator(workspace)
        manifest = Manifest.from_file(workspace / "manifests" / "test.yaml")
        site = orchestrator.load_site("test-site")
        step = manifest.steps[0]

        result = orchestrator.resolve_parameters(step, site, manifest, {})

        assert result["location"] == "eastus"
        assert result["environment"] == "dev"
        assert result["clusterName"] == "arc-dev"

    def test_parameters_filtered_to_template_accepted(self, tmp_path):
        """Test that parameters are filtered to what the template accepts."""
        workspace = self._setup_workspace(tmp_path)

        self._create_site(
            workspace,
            """
apiVersion: siteops/v1
kind: Site
name: test-site
subscription: "00000000-0000-0000-0000-000000000000"
resourceGroup: rg-test
location: eastus
""",
        )

        (workspace / "parameters" / "common.yaml").write_text(
            "location: eastus\nextraManifestParam: should-be-filtered\n"
        )
        (workspace / "parameters" / "step.yaml").write_text("name: my-resource\nextraStepParam: also-filtered\n")

        self._create_template(
            workspace,
            {"location": {"type": "string"}, "name": {"type": "string"}},
        )

        (workspace / "manifests" / "test.yaml").write_text(
            """
apiVersion: siteops/v1
kind: Manifest
name: test
sites: [test-site]
parameters: [parameters/common.yaml]
steps:
  - name: test-step
    template: templates/test.json
    parameters: [parameters/step.yaml]
"""
        )

        from siteops.executor import get_template_parameters

        get_template_parameters.cache_clear()

        orchestrator = Orchestrator(workspace)
        manifest = Manifest.from_file(workspace / "manifests" / "test.yaml")
        site = orchestrator.load_site("test-site")
        step = manifest.steps[0]

        result = orchestrator.resolve_parameters(step, site, manifest, {})

        assert result == {"location": "eastus", "name": "my-resource"}
        assert "extraManifestParam" not in result
        assert "extraStepParam" not in result

    def test_site_parameters_included_in_merge(self, tmp_path):
        """Test that site.parameters are included in the merge."""
        workspace = self._setup_workspace(tmp_path)

        self._create_site(
            workspace,
            """
apiVersion: siteops/v1
kind: Site
name: test-site
subscription: "00000000-0000-0000-0000-000000000000"
resourceGroup: rg-test
location: eastus
parameters:
  siteParam: from-site
  sharedParam: site-value
""",
        )

        (workspace / "parameters" / "common.yaml").write_text("sharedParam: manifest-value\n")

        self._create_template(
            workspace,
            {
                "siteParam": {"type": "string"},
                "sharedParam": {"type": "string"},
            },
        )

        (workspace / "manifests" / "test.yaml").write_text(
            """
apiVersion: siteops/v1
kind: Manifest
name: test
sites: [test-site]
parameters: [parameters/common.yaml]
steps:
  - name: test-step
    template: templates/test.json
"""
        )

        from siteops.executor import get_template_parameters

        get_template_parameters.cache_clear()

        orchestrator = Orchestrator(workspace)
        manifest = Manifest.from_file(workspace / "manifests" / "test.yaml")
        site = orchestrator.load_site("test-site")
        step = manifest.steps[0]

        result = orchestrator.resolve_parameters(step, site, manifest, {})

        assert result["siteParam"] == "from-site"
        # Manifest params override site params
        assert result["sharedParam"] == "manifest-value"

    def test_missing_manifest_parameter_file_logs_warning(self, tmp_path, caplog):
        """Test that missing manifest parameter file logs a warning."""
        workspace = self._setup_workspace(tmp_path)

        self._create_site(
            workspace,
            """
apiVersion: siteops/v1
kind: Site
name: test-site
subscription: "00000000-0000-0000-0000-000000000000"
resourceGroup: rg-test
location: eastus
""",
        )

        self._create_template(workspace, {})

        (workspace / "manifests" / "test.yaml").write_text(
            """
apiVersion: siteops/v1
kind: Manifest
name: test
sites: [test-site]
parameters: [parameters/nonexistent.yaml]
steps:
  - name: test-step
    template: templates/test.json
"""
        )

        from siteops.executor import get_template_parameters

        get_template_parameters.cache_clear()

        orchestrator = Orchestrator(workspace)
        manifest = Manifest.from_file(workspace / "manifests" / "test.yaml")
        site = orchestrator.load_site("test-site")
        step = manifest.steps[0]

        with caplog.at_level(logging.WARNING):
            orchestrator.resolve_parameters(step, site, manifest, {})

        assert any("not found" in record.message.lower() for record in caplog.records)

    def test_deep_merge_for_manifest_parameters(self, tmp_path):
        """Test that manifest parameters use deep merge for nested objects."""
        workspace = self._setup_workspace(tmp_path)

        self._create_site(
            workspace,
            """
apiVersion: siteops/v1
kind: Site
name: test-site
subscription: "00000000-0000-0000-0000-000000000000"
resourceGroup: rg-test
location: eastus
""",
        )

        # First manifest params file with base values
        (workspace / "parameters" / "common.yaml").write_text(
            """
tags:
  managedBy: siteops
  team: platform
config:
  retries: 3
"""
        )

        # Second manifest params file that extends
        (workspace / "parameters" / "shared.yaml").write_text(
            """
tags:
  environment: dev
config:
  timeout: 30
"""
        )

        self._create_template(
            workspace,
            {
                "tags": {"type": "object"},
                "config": {"type": "object"},
            },
        )

        (workspace / "manifests" / "test.yaml").write_text(
            """
apiVersion: siteops/v1
kind: Manifest
name: test
sites: [test-site]
parameters:
  - parameters/common.yaml
  - parameters/shared.yaml
steps:
  - name: test-step
    template: templates/test.json
"""
        )

        from siteops.executor import get_template_parameters

        get_template_parameters.cache_clear()

        orchestrator = Orchestrator(workspace)
        manifest = Manifest.from_file(workspace / "manifests" / "test.yaml")
        site = orchestrator.load_site("test-site")
        step = manifest.steps[0]

        result = orchestrator.resolve_parameters(step, site, manifest, {})

        # Deep merge should combine nested objects
        assert result["tags"] == {
            "managedBy": "siteops",
            "team": "platform",
            "environment": "dev",
        }
        assert result["config"] == {
            "retries": 3,
            "timeout": 30,
        }


class TestParametersResolution:
    """Tests for site.parameters template resolution."""

    def _setup_workspace(self, tmp_path):
        """Create standard workspace structure."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "parameters").mkdir()
        (workspace / "templates").mkdir()
        (workspace / "sites").mkdir()
        (workspace / "manifests").mkdir()
        return workspace

    def _create_site(self, workspace, content):
        """Create site file."""
        site_file = workspace / "sites" / "test-site.yaml"
        site_file.write_text(content)

    def _create_template(self, workspace, params):
        """Create ARM JSON template with specified parameters."""
        template_file = workspace / "templates" / "test.json"
        template_file.write_text(json.dumps({"parameters": params}))

    def test_resolve_simple_parameter(self, tmp_workspace):
        orchestrator = Orchestrator(workspace=tmp_workspace)
        site = Site(
            name="test-site",
            subscription="sub-123",
            resource_group="rg-test",
            location="eastus",
            parameters={"clusterName": "my-arc-cluster"},
        )

        result = orchestrator._resolve_template_strings("{{ site.parameters.clusterName }}", site)
        assert result == "my-arc-cluster"

    def test_resolve_nested_parameter(self, tmp_workspace):
        """Test resolving a nested site parameter."""
        orchestrator = Orchestrator(workspace=tmp_workspace)
        site = Site(
            name="test-site",
            subscription="sub-123",
            resource_group="rg-test",
            location="eastus",
            parameters={
                "brokerConfig": {
                    "memoryProfile": "Medium",
                    "frontendReplicas": 2,
                }
            },
        )

        result = orchestrator._resolve_template_strings("{{ site.parameters.brokerConfig.memoryProfile }}", site)
        assert result == "Medium"

    def test_resolve_entire_object_parameter(self, tmp_workspace):
        """Test resolving an entire object parameter."""
        orchestrator = Orchestrator(workspace=tmp_workspace)
        site = Site(
            name="test-site",
            subscription="sub-123",
            resource_group="rg-test",
            location="eastus",
            parameters={
                "brokerConfig": {
                    "memoryProfile": "Medium",
                    "frontendReplicas": 2,
                }
            },
        )

        result = orchestrator._resolve_template_strings("{{ site.parameters.brokerConfig }}", site)
        assert result == {"memoryProfile": "Medium", "frontendReplicas": 2}

    def test_resolve_parameter_embedded_in_string(self, tmp_workspace):
        """Test resolving a parameter embedded in a string."""
        orchestrator = Orchestrator(workspace=tmp_workspace)
        site = Site(
            name="test-site",
            subscription="sub-123",
            resource_group="rg-test",
            location="eastus",
            parameters={"clusterName": "my-cluster", "customLocationName": "my-cl"},
        )

        result = orchestrator._resolve_template_strings(
            "Cluster: {{ site.parameters.clusterName }}, Location: {{ site.parameters.customLocationName }}",
            site,
        )
        assert result == "Cluster: my-cluster, Location: my-cl"

    def test_resolve_missing_parameter_unchanged(self, tmp_workspace):
        """Test that missing parameters are left unchanged."""
        orchestrator = Orchestrator(workspace=tmp_workspace)
        site = Site(
            name="test-site",
            subscription="sub-123",
            resource_group="rg-test",
            location="eastus",
            parameters={},
        )

        result = orchestrator._resolve_template_strings("{{ site.parameters.nonexistent }}", site)
        assert result == "{{ site.parameters.nonexistent }}"

    def test_resolve_parameter_in_nested_dict(self, tmp_workspace):
        """Test resolving parameters in nested dict structures."""
        orchestrator = Orchestrator(workspace=tmp_workspace)
        site = Site(
            name="test-site",
            subscription="sub-123",
            resource_group="rg-test",
            location="eastus",
            parameters={"clusterName": "my-cluster"},
        )

        value = {
            "resourceId": "/subscriptions/{{ site.subscription }}/clusters/{{ site.parameters.clusterName }}",
            "nested": {
                "cluster": "{{ site.parameters.clusterName }}",
            },
        }
        result = orchestrator._resolve_template_strings(value, site)

        assert result["resourceId"] == "/subscriptions/sub-123/clusters/my-cluster"
        assert result["nested"]["cluster"] == "my-cluster"

    def test_resolve_parameter_in_list(self, tmp_workspace):
        """Test resolving parameters in list structures."""
        orchestrator = Orchestrator(workspace=tmp_workspace)
        site = Site(
            name="test-site",
            subscription="sub-123",
            resource_group="rg-test",
            location="eastus",
            parameters={"clusterName": "my-cluster"},
        )

        value = ["{{ site.parameters.clusterName }}", "static", "{{ site.name }}"]
        result = orchestrator._resolve_template_strings(value, site)

        assert result == ["my-cluster", "static", "test-site"]

    def test_resolve_entire_array_parameter(self, tmp_workspace):
        """Test resolving an entire array parameter."""
        orchestrator = Orchestrator(workspace=tmp_workspace)
        site = Site(
            name="test-site",
            subscription="sub-123",
            resource_group="rg-test",
            location="eastus",
            parameters={
                "endpoints": [
                    {"host": "10.0.1.100", "port": 4840},
                    {"host": "10.0.1.101", "port": 4840},
                ]
            },
        )

        result = orchestrator._resolve_template_strings("{{ site.parameters.endpoints }}", site)
        assert result == [
            {"host": "10.0.1.100", "port": 4840},
            {"host": "10.0.1.101", "port": 4840},
        ]

    def test_resolve_parameter_with_overlay(self, tmp_workspace):
        """Test that parameters from overlay are resolved correctly."""
        # Create base site
        (tmp_workspace / "sites" / "test-site.yaml").write_text(
            """
apiVersion: siteops/v1
kind: Site
name: test-site
subscription: "00000000-0000-0000-0000-000000000000"
resourceGroup: rg-test
location: eastus
parameters:
  clusterName: base-cluster
"""
        )

        # Create overlay with parameter override
        (tmp_workspace / "sites.local").mkdir(exist_ok=True)
        (tmp_workspace / "sites.local" / "test-site.yaml").write_text(
            """
parameters:
  clusterName: overlay-cluster
"""
        )

        orchestrator = Orchestrator(workspace=tmp_workspace)
        site = orchestrator.load_site("test-site")

        result = orchestrator._resolve_template_strings("{{ site.parameters.clusterName }}", site)
        assert result == "overlay-cluster"

    def test_site_parameters_template_in_manifest_params(self, tmp_path):
        """Test that {{ site.parameters.X }} in manifest params are resolved."""
        workspace = self._setup_workspace(tmp_path)

        self._create_site(
            workspace,
            """
apiVersion: siteops/v1
kind: Site
name: test-site
subscription: "00000000-0000-0000-0000-000000000000"
resourceGroup: rg-test
location: eastus
parameters:
  clusterName: my-arc-cluster
  customLocationName: my-cl
""",
        )

        # Parameter file uses {{ site.parameters.X }}
        (workspace / "parameters" / "common.yaml").write_text(
            """
clusterName: "{{ site.parameters.clusterName }}"
customLocationName: "{{ site.parameters.customLocationName }}"
resourceId: "/subscriptions/{{ site.subscription }}/clusters/{{ site.parameters.clusterName }}"
"""
        )

        self._create_template(
            workspace,
            {
                "clusterName": {"type": "string"},
                "customLocationName": {"type": "string"},
                "resourceId": {"type": "string"},
            },
        )

        (workspace / "manifests" / "test.yaml").write_text(
            """
apiVersion: siteops/v1
kind: Manifest
name: test
sites: [test-site]
parameters: [parameters/common.yaml]
steps:
  - name: test-step
    template: templates/test.json
"""
        )

        from siteops.executor import get_template_parameters

        get_template_parameters.cache_clear()

        orchestrator = Orchestrator(workspace)
        manifest = Manifest.from_file(workspace / "manifests" / "test.yaml")
        site = orchestrator.load_site("test-site")
        step = manifest.steps[0]

        result = orchestrator.resolve_parameters(step, site, manifest, {})

        assert result["clusterName"] == "my-arc-cluster"
        assert result["customLocationName"] == "my-cl"
        assert result["resourceId"] == "/subscriptions/00000000-0000-0000-0000-000000000000/clusters/my-arc-cluster"

    def test_site_overlay_parameters_resolved_in_manifest_params(self, tmp_path):
        """Test that site overlay parameters are resolved in manifest parameter files.

        This is the exact scenario that failed in CI: SITE_OVERRIDES creates
        sites.local/site.yaml with parameters.clusterName override, and
        manifest parameters reference {{ site.parameters.clusterName }}.
        """
        workspace = self._setup_workspace(tmp_path)

        # Base site with placeholder values
        self._create_site(
            workspace,
            """
apiVersion: siteops/v1
kind: Site
name: test-site
subscription: "00000000-0000-0000-0000-000000000000"
resourceGroup: rg-test
location: eastus
parameters:
  clusterName: placeholder-cluster
  customLocationName: placeholder-cl
""",
        )

        # Local overlay (simulates SITE_OVERRIDES in CI)
        (workspace / "sites.local").mkdir(exist_ok=True)
        (workspace / "sites.local" / "test-site.yaml").write_text(
            """
parameters:
  clusterName: real-cluster-from-overlay
"""
        )

        # Parameter file references site parameters
        (workspace / "parameters" / "common.yaml").write_text(
            """
clusterName: "{{ site.parameters.clusterName }}"
customLocationName: "{{ site.parameters.customLocationName }}"
"""
        )

        self._create_template(
            workspace,
            {
                "clusterName": {"type": "string"},
                "customLocationName": {"type": "string"},
            },
        )

        (workspace / "manifests" / "test.yaml").write_text(
            """
apiVersion: siteops/v1
kind: Manifest
name: test
sites: [test-site]
parameters: [parameters/common.yaml]
steps:
  - name: test-step
    template: templates/test.json
"""
        )

        from siteops.executor import get_template_parameters

        get_template_parameters.cache_clear()

        orchestrator = Orchestrator(workspace)
        manifest = Manifest.from_file(workspace / "manifests" / "test.yaml")
        site = orchestrator.load_site("test-site")
        step = manifest.steps[0]

        result = orchestrator.resolve_parameters(step, site, manifest, {})

        # Overlay value should be used, not placeholder
        assert result["clusterName"] == "real-cluster-from-overlay"
        # Non-overridden value preserved from base
        assert result["customLocationName"] == "placeholder-cl"
