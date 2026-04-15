# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Core data models for Azure Site Ops.

This module defines the core resource types:
- Site: A deployment target (subscription, resource group, location)
- Manifest: Orchestrates deployment steps across sites
- DeploymentStep: A single Bicep/ARM template deployment
- KubectlStep: A kubectl operation against an Arc-connected cluster

Resources support K8s-style apiVersion/kind validation:
- apiVersion defaults to 'siteops/v1' if not specified
- kind is validated if present, but optional
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import re
import yaml

VALID_SCOPES = {"subscription", "resourceGroup"}
DEFAULT_API_VERSION = "siteops/v1"
SUPPORTED_API_VERSIONS = {"siteops/v1"}

# Pattern for condition expressions in 'when' clauses
# Supports:
#   - Comparison: site.labels.<key> == 'value' or site.properties.<path> != 'value'
#   - Boolean shorthand: site.properties.<path> (truthy check)
# Values can be quoted strings ('value' or "value") or unquoted booleans (true/false)
CONDITION_PATTERN = re.compile(
    r"\{\{\s*site\.(labels\.[a-zA-Z0-9_-]+|properties\.[a-zA-Z0-9_.\[\]-]+)"
    r"(?:\s*(==|!=)\s*(?:['\"]([^'\"]*?)['\"]|(true|false)))?\s*\}\}"
)

# Supported kubectl operations (extensible for future operations like 'wait', 'delete')
KUBECTL_OPERATIONS = {"apply"}


def parse_selector(selector: str | None) -> dict[str, str]:
    """Parse a label selector string into key-value pairs.

    Args:
        selector: Comma-separated key=value pairs (e.g., 'environment=prod,region=eastus'),
                  or None/empty string for no filtering.

    Returns:
        Dict of label key-value pairs (empty dict if selector is None/empty)

    Example:
        >>> parse_selector('environment=prod,region=eastus')
        {'environment': 'prod', 'region': 'eastus'}
        >>> parse_selector(None)
        {}
    """
    if not selector:
        return {}

    labels = {}
    for part in selector.split(","):
        part = part.strip()
        if "=" in part:
            key, value = part.split("=", 1)
            labels[key.strip()] = value.strip()
    return labels


def _validate_resource(data: dict[str, Any], expected_kind: str | list[str], path: Path) -> str:
    """Validate apiVersion and kind for a resource file.

    Args:
        data: Parsed YAML data
        expected_kind: The expected kind(s) (e.g., 'Site' or ['Site', 'SiteTemplate'])
        path: File path for error messages

    Returns:
        The validated apiVersion string

    Raises:
        ValueError: If kind doesn't match expected or apiVersion is unsupported

    Note:
        - apiVersion defaults to 'siteops/v1' if not specified
        - kind is only validated if present; if omitted, the resource type
          is determined by the calling context
    """
    api_version = data.get("apiVersion", DEFAULT_API_VERSION)
    kind = data.get("kind")

    # Normalize expected_kind to a list for consistent handling
    expected_kinds = [expected_kind] if isinstance(expected_kind, str) else list(expected_kind)

    if api_version not in SUPPORTED_API_VERSIONS:
        supported = ", ".join(sorted(SUPPORTED_API_VERSIONS))
        raise ValueError(f"Unsupported apiVersion '{api_version}' in {path}. Supported: {supported}")

    if kind is not None and kind not in expected_kinds:
        if len(expected_kinds) == 1:
            raise ValueError(f"Invalid kind '{kind}' in {path}. Expected '{expected_kinds[0]}'")
        else:
            expected_str = ", ".join(f"'{k}'" for k in expected_kinds)
            raise ValueError(f"Invalid kind '{kind}' in {path}. Expected one of: {expected_str}")

    return api_version


@dataclass(frozen=True)
class ParallelConfig:
    """Configuration for parallel site execution.

    Controls how many sites are deployed concurrently during manifest execution.

    Attributes:
        sites: Maximum concurrent sites.
            - 0 means unlimited (all sites run concurrently)
            - 1 means sequential (one site at a time)
            - N means at most N sites run concurrently

    Examples:
        >>> ParallelConfig.from_value(3)
        ParallelConfig(sites=3)
        >>> ParallelConfig.from_value(True)
        ParallelConfig(sites=0)
        >>> ParallelConfig.from_value({"sites": 2})
        ParallelConfig(sites=2)
    """

    sites: int = 1

    def __post_init__(self) -> None:
        """Validate configuration after initialization."""
        if self.sites < 0:
            raise ValueError(f"parallel.sites must be >= 0, got {self.sites}")

    @classmethod
    def from_value(cls, value: Any) -> "ParallelConfig":
        """Parse parallel config from a manifest value.

        Args:
            value: One of:
                - None: Returns default (sequential)
                - bool: True = unlimited, False = sequential
                - int: Max concurrent sites (0 = unlimited)
                - dict: Object form with 'sites' key

        Returns:
            Configured ParallelConfig instance

        Raises:
            ValueError: If value is invalid type or out of range

        Examples:
            parallel: 3           -> ParallelConfig(sites=3)
            parallel: 0           -> ParallelConfig(sites=0)  # unlimited
            parallel: true        -> ParallelConfig(sites=0)  # unlimited
            parallel: false       -> ParallelConfig(sites=1)  # sequential
            parallel:
              sites: 3            -> ParallelConfig(sites=3)
        """
        if value is None:
            return cls()

        if isinstance(value, bool):
            return cls(sites=0 if value else 1)

        if isinstance(value, int):
            return cls(sites=value)

        if isinstance(value, dict):
            sites = value.get("sites", 1)
            if not isinstance(sites, int):
                raise ValueError(f"parallel.sites must be an integer, got {type(sites).__name__}")
            return cls(sites=sites)

        raise ValueError(f"Invalid parallel value: expected bool, int, or dict, " f"got {type(value).__name__}")

    @property
    def is_sequential(self) -> bool:
        """Return True if deployment runs one site at a time."""
        return self.sites == 1

    @property
    def is_unlimited(self) -> bool:
        """Return True if all sites run concurrently."""
        return self.sites == 0

    @property
    def max_workers(self) -> int | None:
        """Return max workers for ThreadPoolExecutor, or None for unlimited."""
        return None if self.sites == 0 else self.sites

    def __str__(self) -> str:
        """Return human-readable description."""
        if self.is_unlimited:
            return "unlimited"
        if self.is_sequential:
            return "sequential"
        return f"max {self.sites}"


@dataclass
class Site:
    """Deployment target representing an Azure subscription/resource group.

    Attributes:
        name: Unique identifier for the site
        subscription: Azure subscription ID
        resource_group: Azure resource group name
        location: Azure region (e.g., 'eastus', 'westus2')
        labels: Key-value string pairs for filtering with selectors
        properties: Structured data for complex site-specific configuration
        parameters: Default parameters to include in all deployments to this site
    """

    name: str
    subscription: str
    resource_group: str
    location: str
    labels: dict[str, str] = field(default_factory=dict)
    properties: dict[str, Any] = field(default_factory=dict)
    parameters: dict[str, Any] = field(default_factory=dict)

    def matches_selector(self, selector: dict[str, str]) -> bool:
        """Check if site matches all selector criteria.

        Supports:
        - name=<value>: Match site name exactly
        - <label>=<value>: Match label value

        Args:
            selector: Dictionary of key=value pairs to match

        Returns:
            True if all selector criteria match
        """
        for key, value in selector.items():
            if key == "name":
                # Special case: match site name
                if self.name != value:
                    return False
            else:
                # Match against labels
                if self.labels.get(key) != value:
                    return False
        return True

    @classmethod
    def from_file(cls, path: Path) -> "Site":
        """Load a site from a YAML file.

        Supports two formats:
        1. Flat format (recommended):
            ```yaml
            apiVersion: siteops/v1
            kind: Site
            name: dev-eastus
            subscription: "..."
            resourceGroup: "..."
            location: eastus
            labels:
              environment: dev
            properties:
              deviceEndpoints:
                - host: 10.0.1.100
                  port: 4840
            ```

        2. K8s-style nested format:
            ```yaml
            apiVersion: siteops/v1
            kind: Site
            metadata:
              name: dev-eastus
              labels:
                environment: dev
            spec:
              subscription: "..."
              resourceGroup: "..."
              location: eastus
              properties:
                deviceEndpoints:
                  - host: 10.0.1.100
                    port: 4840
            ```

        Args:
            path: Path to the YAML file

        Returns:
            Site instance

        Raises:
            ValueError: If file is empty, invalid, or missing required fields
        """
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        if not data:
            raise ValueError(f"Empty or invalid YAML file: {path}")

        _validate_resource(data, "Site", path)

        if "spec" in data:
            spec = data["spec"]
            metadata = data.get("metadata", {})
            name = metadata.get("name", path.stem)
            labels = metadata.get("labels", {})
        else:
            spec = data
            name = data.get("name", path.stem)
            labels = data.get("labels", {})

        required = ["subscription", "location"]
        for req in required:
            if req not in spec:
                raise ValueError(f"Missing required field '{req}' in site: {path}")

        return cls(
            name=name,
            subscription=spec["subscription"],
            resource_group=spec.get("resourceGroup", ""),
            location=spec["location"],
            labels=labels,
            properties=spec.get("properties", {}),
            parameters=spec.get("parameters", {}),
        )

    @property
    def is_subscription_level(self) -> bool:
        """Check if this is a subscription-level site (no resource group).

        Subscription-level sites are used for deploying shared resources
        once per subscription (e.g., Azure Edge Sites). They have only
        subscription + location, no resourceGroup.

        Returns:
            True if site has no resource_group (subscription-level)
            False if site has a resource_group (RG-level)
        """
        return not self.resource_group

    def get_all_parameters(self) -> dict[str, Any]:
        """Get a copy of site-level parameters.

        Returns:
            Copy of the parameters dict (modifications won't affect the site)
        """
        return dict(self.parameters)

    def __repr__(self) -> str:
        return f"Site(name={self.name!r}, location={self.location!r})"


@dataclass
class DeploymentStep:
    """A single Bicep/ARM deployment step within a manifest.

    Attributes:
        name: Unique name for the step (used in deployment names and output references)
        template: Path to the Bicep/ARM template file (relative to workspace)
        parameters: List of parameter file paths (relative to workspace)
        scope: Deployment scope - 'resourceGroup' or 'subscription'
        when: Optional condition expression (e.g., "{{ site.labels.X == 'Y' }}")
    """

    name: str
    template: str
    parameters: list[str] = field(default_factory=list)
    scope: str = "resourceGroup"
    when: str | None = None

    def __post_init__(self) -> None:
        if self.scope not in VALID_SCOPES:
            raise ValueError(f"Invalid scope '{self.scope}'. Must be one of: {VALID_SCOPES}")

        if self.when and not CONDITION_PATTERN.fullmatch(self.when.strip()):
            raise ValueError(
                f"Invalid 'when' condition syntax: {self.when}. "
                "Expected: {{ site.labels.X == 'value' }}, {{ site.properties.path == true }}, "
                "or {{ site.properties.path }} (truthy check)"
            )


@dataclass
class ArcCluster:
    """Arc-connected Kubernetes cluster configuration.

    Attributes:
        name: Cluster name (supports template variables like {{ site.labels.clusterName }})
        resource_group: Resource group containing the cluster (supports template variables)
    """

    name: str
    resource_group: str


@dataclass
class KubectlStep:
    """A kubectl operation step within a manifest.

    Executes kubectl commands against an Arc-connected Kubernetes cluster.
    Site Ops automatically manages the `az connectedk8s proxy` lifecycle.

    Attributes:
        name: Unique name for the step
        operation: Kubectl operation ('apply' is currently supported)
        arc: Arc cluster configuration (name and resourceGroup)
        files: List of file paths (relative to workspace) or HTTPS URLs to apply
        when: Optional condition expression (e.g., "{{ site.labels.X == 'Y' }}")

    Example manifest usage:
        ```yaml
        - name: apply-config
          type: kubectl
          operation: apply
          arc:
            name: "{{ site.labels.clusterName }}"
            resourceGroup: "{{ site.resourceGroup }}"
          files:
            - https://example.com/manifest.yaml
            - configs/local-config.yaml
          when: "{{ site.labels.enableConfig == 'true' }}"
        ```
    """

    name: str
    operation: str
    arc: ArcCluster
    files: list[str] = field(default_factory=list)
    when: str | None = None

    def __post_init__(self) -> None:
        if self.operation not in KUBECTL_OPERATIONS:
            raise ValueError(
                f"Invalid kubectl operation '{self.operation}'. " f"Supported: {', '.join(sorted(KUBECTL_OPERATIONS))}"
            )

        if not self.files:
            raise ValueError(f"KubectlStep '{self.name}' must specify at least one file")

        if self.when and not CONDITION_PATTERN.fullmatch(self.when.strip()):
            raise ValueError(
                f"Invalid 'when' condition syntax: {self.when}. "
                "Expected: {{ site.labels.X == 'value' }}, {{ site.properties.path == true }}, "
                "or {{ site.properties.path }} (truthy check)"
            )


# Union type for manifest steps - allows type checking to distinguish step types
ManifestStep = DeploymentStep | KubectlStep


@dataclass
class Manifest:
    """Deployment manifest that orchestrates templates across sites.

    A manifest defines:
    - Which sites to deploy to (explicit list or label selector)
    - What steps to execute (Bicep/ARM deployments or kubectl operations)
    - The order of deployment (steps execute sequentially per site)
    - Whether to deploy to sites in parallel
    - Shared parameters applied to all steps (with auto-filtering)

    Attributes:
        name: Unique identifier for the manifest
        description: Human-readable description
        sites: Explicit list of site names to deploy to
        steps: Ordered list of steps (DeploymentStep or KubectlStep)
        site_selector: Label selector string (e.g., 'environment=prod')
        parallel: Parallelization config (int, bool, or object with 'sites' key)
        parameters: Manifest-level parameter files applied to all steps

    Parallel Configuration:
        - parallel: 0           # Unlimited concurrency (all sites at once)
        - parallel: 1           # Sequential (one site at a time, default)
        - parallel: 3           # Max 3 sites concurrently
        - parallel: true        # Unlimited concurrency
        - parallel: false       # Sequential
        - parallel:
            sites: 3            # Object form, max 3 sites concurrently
    """

    name: str
    description: str
    sites: list[str]
    steps: list[ManifestStep]
    site_selector: str | None = None
    parallel: ParallelConfig = field(default_factory=ParallelConfig)
    parameters: list[str] = field(default_factory=list)

    @classmethod
    def from_file(cls, path: Path) -> "Manifest":
        """Load a manifest from a YAML file.

        Example manifest:
            ```yaml
            apiVersion: siteops/v1
            kind: Manifest
            name: iot-operations
            description: Deploy Azure IoT Operations
            parallel: 2  # Max 2 sites concurrently

            sites:
              - dev-eastus

            steps:
              - name: aio-enablement
                template: templates/enablement.bicep
                scope: subscription
                parameters:
                  - parameters/enablement.yaml

              - name: configure-cluster
                type: kubectl
                operation: apply
                arc:
                  name: "{{ site.labels.clusterName }}"
                  resourceGroup: "{{ site.resourceGroup }}"
                files:
                  - https://example.com/config.yaml
                when: "{{ site.labels.enableConfig == 'true' }}"
            ```

        Args:
            path: Path to the YAML file

        Returns:
            Manifest instance

        Raises:
            ValueError: If file is empty, invalid, or steps are misconfigured
        """
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        if not data:
            raise ValueError(f"Empty or invalid YAML file: {path}")

        _validate_resource(data, "Manifest", path)

        if "spec" in data:
            metadata = data.get("metadata", {})
            spec = data["spec"]
            name = metadata.get("name", path.stem)
            description = metadata.get("description", "")
        else:
            spec = data
            name = data.get("name", path.stem)
            description = data.get("description", "")

        sites = []
        for item in spec.get("sites", []):
            if isinstance(item, str):
                sites.append(item)

        site_selector = spec.get("siteSelector")
        parallel = ParallelConfig.from_value(spec.get("parallel"))

        steps: list[ManifestStep] = []
        for i, step_data in enumerate(spec.get("steps", [])):
            if "name" not in step_data:
                raise ValueError(f"Step {i+1} missing required field 'name' in manifest: {path}")

            step_type = step_data.get("type", "deployment")

            if step_type == "kubectl":
                if "operation" not in step_data:
                    raise ValueError(
                        f"Step '{step_data['name']}' (type: kubectl) missing 'operation' in manifest: {path}"
                    )
                if "arc" not in step_data:
                    raise ValueError(
                        f"Step '{step_data['name']}' (type: kubectl) missing 'arc' configuration in manifest: {path}"
                    )
                arc_data = step_data["arc"]
                if "name" not in arc_data or "resourceGroup" not in arc_data:
                    raise ValueError(
                        f"Step '{step_data['name']}' arc config must have 'name' and 'resourceGroup': {path}"
                    )
                if "files" not in step_data or not step_data["files"]:
                    raise ValueError(f"Step '{step_data['name']}' (type: kubectl) missing 'files' in manifest: {path}")

                steps.append(
                    KubectlStep(
                        name=step_data["name"],
                        operation=step_data["operation"],
                        arc=ArcCluster(
                            name=arc_data["name"],
                            resource_group=arc_data["resourceGroup"],
                        ),
                        files=step_data["files"],
                        when=step_data.get("when"),
                    )
                )
            else:
                if "template" not in step_data:
                    raise ValueError(f"Step '{step_data['name']}' missing 'template' in manifest: {path}")
                steps.append(
                    DeploymentStep(
                        name=step_data["name"],
                        template=step_data["template"],
                        parameters=step_data.get("parameters", []),
                        scope=step_data.get("scope", "resourceGroup"),
                        when=step_data.get("when"),
                    )
                )

        return cls(
            name=name,
            description=description,
            sites=sites,
            steps=steps,
            site_selector=site_selector,
            parallel=parallel,
            parameters=spec.get("parameters", []),
        )

    def resolve_parameter_path(self, param_path: str, site: "Site") -> str:
        """Resolve template variables in a parameter file path.

        Supports:
        - {{ site.name }} - Site name
        - {{ site.location }} - Site location
        - {{ site.resourceGroup }} - Site resource group
        - {{ site.subscription }} - Site subscription
        - {{ site.labels.<key> }} - Site label value
        - {{ site.properties.<path> }} - Site property value (nested paths supported)

        Args:
            param_path: Parameter file path with optional template variables
            site: Site to resolve variables from

        Returns:
            Resolved path string
        """
        result = param_path
        result = result.replace("{{ site.name }}", site.name)
        result = result.replace("{{ site.location }}", site.location)
        result = result.replace("{{ site.resourceGroup }}", site.resource_group)
        result = result.replace("{{ site.subscription }}", site.subscription)

        for key, value in site.labels.items():
            result = result.replace(f"{{{{ site.labels.{key} }}}}", value)

        # Resolve {{ site.properties.<path> }} templates
        for match in re.finditer(r"\{\{\s*site\.properties\.(\S+?)\s*\}\}", result):
            prop_path = match.group(1)
            value = site.properties
            for part in prop_path.split("."):
                if isinstance(value, dict) and part in value:
                    value = value[part]
                else:
                    value = None
                    break
            if value is not None:
                result = result.replace(match.group(0), str(value))

        return result
