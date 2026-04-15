# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Main orchestration engine.

This module provides the Orchestrator class which handles:
- Loading sites and manifests from the workspace
- Resolving parameters with template variable substitution
- Executing deployment steps (Bicep/ARM and kubectl) across sites
- Parallel and sequential deployment modes with configurable concurrency
"""

import copy
import hashlib
import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from siteops.executor import (
    AzCliExecutor,
    DeploymentResult,
    KubectlResult,
    filter_parameters,
)
from siteops.models import (
    CONDITION_PATTERN,
    DeploymentStep,
    KubectlStep,
    Manifest,
    ManifestStep,
    ParallelConfig,
    Site,
    _validate_resource,
    parse_selector,
)

logger = logging.getLogger(__name__)

# Pattern for {{ steps.<step_name>.outputs.<output_path> }}
# Supports nested paths like: steps.X.outputs.Y.Z.A
STEP_OUTPUT_PATTERN = re.compile(r"\{\{\s*steps\.([a-zA-Z0-9_-]+)\.outputs\.([a-zA-Z0-9_.-]+)\s*\}\}")

# Pattern for {{ site.properties.<path> }}
# Supports nested paths and array indices like: site.properties.endpoints[0].host
SITE_PROPERTIES_PATTERN = re.compile(r"\{\{\s*site\.properties\.([a-zA-Z0-9_.\[\]]+)\s*\}\}")

# Pattern for {{ site.parameters.<path> }}
# Supports nested paths like: site.parameters.brokerConfig.memoryProfile
SITE_PARAMETERS_PATTERN = re.compile(r"\{\{\s*site\.parameters\.([a-zA-Z0-9_.\[\]]+)\s*\}\}")

# Result type that can be either a deployment or kubectl result
StepResult = DeploymentResult | KubectlResult

# Type alias for subscription-scoped outputs: subscription_id -> step_name -> outputs
SubscriptionOutputs = dict[str, dict[str, dict[str, Any]]]


def _resolve_output_path(obj: Any, path: str) -> Any:
    """Resolve a dot-separated path into an object.

    Handles Azure ARM output format which wraps values in {"value": X, "type": "..."}

    Args:
        obj: The object to traverse (dict or value)
        path: Dot-separated path like "adrNamespace.id"

    Returns:
        The value at the path, or None if not found
    """
    parts = path.split(".")
    current = obj

    for part in parts:
        if current is None:
            return None
        # Unwrap Azure output format at each level
        if isinstance(current, dict) and "value" in current and "type" in current:
            current = current["value"]
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None

    # Final unwrap if needed
    if isinstance(current, dict) and "value" in current and "type" in current:
        current = current["value"]

    return current


# Lock for thread-safe console output
_print_lock = threading.Lock()


def _thread_safe_print(*args: Any, **kwargs: Any) -> None:
    """Print with lock to avoid interleaved output from multiple threads."""
    with _print_lock:
        print(*args, **kwargs)


class Orchestrator:
    """Orchestrates deployments across sites.

    The orchestrator is responsible for:
    - Loading and caching sites from the workspace
    - Resolving manifest steps with parameter files and template variables
    - Executing deployment steps (Bicep/ARM deployments and kubectl operations)
    - Managing parallel deployment to multiple sites with configurable concurrency

    Attributes:
        workspace: Path to the Site Ops workspace directory
        dry_run: If True, commands are logged but not executed
        executor: The AzCliExecutor instance for running commands
    """

    def __init__(self, workspace: Path, dry_run: bool = False):
        self.workspace = Path(workspace).resolve()
        self.dry_run = dry_run
        self.executor = AzCliExecutor(workspace=self.workspace, dry_run=dry_run)
        self._params_cache: dict[Path, dict[str, Any]] = {}
        self._params_cache_lock = threading.Lock()
        self._site_cache: dict[str, Site] = {}
        self._cache_lock = threading.Lock()

    def _deep_merge(self, base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
        """Deep merge two dictionaries, with override taking precedence.

        Behavior:
        - Nested dicts are merged recursively
        - Lists are REPLACED entirely (not concatenated)
        - Scalar values from override replace base values

        Args:
            base: Base dictionary
            override: Override dictionary (values take precedence)

        Returns:
            New merged dictionary (neither input is modified)

        Example:
            >>> base = {"a": {"x": 1, "y": 2}, "b": [1, 2]}
            >>> override = {"a": {"x": 10}, "b": [3]}
            >>> _deep_merge(base, override)
            {"a": {"x": 10, "y": 2}, "b": [3]}  # Note: list replaced, not merged
        """
        result = copy.deepcopy(base)
        for key, value in override.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = self._deep_merge(result[key], value)
            else:
                result[key] = copy.deepcopy(value)
        return result

    def _load_inherited_data(self, path: Path, seen: list[Path] | None = None) -> dict[str, Any]:
        """Load inherited site template with support for chained inheritance.

        Resolves the `inherits` field recursively, merging parent data first.

        Args:
            path: Absolute path to the inherited file
            seen: List of visited paths for cycle detection (preserves order)

        Returns:
            Merged data from inheritance chain (with metadata fields stripped)

        Raises:
            FileNotFoundError: If inherited file doesn't exist
            ValueError: If circular inheritance is detected or kind is invalid
        """
        if seen is None:
            seen = []

        # Normalize path for consistent cycle detection
        normalized = path.resolve()
        if normalized in seen:
            cycle_path = " -> ".join(str(p) for p in seen) + f" -> {normalized}"
            raise ValueError(f"Circular inheritance detected: {cycle_path}")
        seen.append(normalized)

        if not path.exists():
            raise FileNotFoundError(f"Inherited file not found: {path}")

        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        # Validate kind if present (allow Site or SiteTemplate)
        kind = data.get("kind")
        if kind is not None and kind not in ("Site", "SiteTemplate"):
            raise ValueError(f"Cannot inherit from kind '{kind}' in {path}. " f"Expected 'Site' or 'SiteTemplate'")

        # Handle chained inheritance
        if "inherits" in data:
            parent_path = (path.parent / data["inherits"]).resolve()
            parent_data = self._load_inherited_data(parent_path, seen)
            # Remove metadata fields before merging
            child_data = {k: v for k, v in data.items() if k not in ("inherits", "kind", "apiVersion")}
            data = self._deep_merge(parent_data, child_data)
        else:
            # Remove metadata fields from leaf template
            data = {k: v for k, v in data.items() if k not in ("kind", "apiVersion")}

        logger.debug(f"Loaded inherited data from: {path}")
        return data

    def _load_site_data(self, name: str) -> dict[str, Any]:
        """Load and merge site data with inheritance and overlay support.

        Merge order (later overrides earlier):
        1. inherits target  - Parent template (if specified, resolved recursively)
        2. sites/           - Base site definitions (committed)
        3. sites.local/     - Local/CI overrides (gitignored)

        Note: Only the base file (sites/) can specify `inherits`. The sites.local/
        overlay cannot change inheritance for security reasons.

        Args:
            name: Site name (filename without extension)

        Returns:
            Merged site data dictionary

        Raises:
            FileNotFoundError: If site file doesn't exist in any directory
            ValueError: If inheritance creates a cycle or references invalid kind
        """
        site_dirs = [
            self.workspace / "sites",  # Base (committed)
            self.workspace / "sites.local",  # Local/CI overrides
        ]

        merged_data: dict[str, Any] = {}
        found = False
        is_base_file = True  # Track if we're processing the base file

        for sites_dir in site_dirs:
            for ext in (".yaml", ".yml"):
                path = sites_dir / f"{name}{ext}"
                if path.exists():
                    with open(path, "r", encoding="utf-8") as f:
                        data = yaml.safe_load(f) or {}

                    # Process inheritance only on base file (not local overlay)
                    if is_base_file and "inherits" in data:
                        inherits_path = (path.parent / data["inherits"]).resolve()
                        # Initialize seen list with current file to detect self-reference
                        inherited_data = self._load_inherited_data(inherits_path, seen=[path.resolve()])
                        merged_data = self._deep_merge(merged_data, inherited_data)
                        # Remove inherits from data before merging
                        data = {k: v for k, v in data.items() if k != "inherits"}
                    elif not is_base_file and "inherits" in data:
                        # Strip inherits from local overlay (security: can't inject inheritance)
                        data = {k: v for k, v in data.items() if k != "inherits"}

                    merged_data = self._deep_merge(merged_data, data)
                    found = True
                    is_base_file = False  # Subsequent files are overlays
                    logger.debug(f"Loaded site data from: {path}")
                    break  # Only load one file per directory (prefer .yaml)

        if not found:
            raise FileNotFoundError(f"Site '{name}' not found in sites/ or sites.local/")

        return merged_data

    def load_site(self, name: str) -> Site:
        """Load a site by name, applying inheritance and local overlays.

        Resolution order (later sources override earlier):
        1. Inherited site/template (if 'inherits' specified)
        2. Base site file from sites/{name}.yaml
        3. Local overlay from sites.local/{name}.yaml (if exists)

        Args:
            name: Site name (corresponds to sites/{name}.yaml)

        Returns:
            Fully resolved Site instance

        Raises:
            ValueError: If site file is invalid, missing required fields,
                       or references non-existent inherited files
            FileNotFoundError: If sites/{name}.yaml doesn't exist
        """
        # Check cache first
        with self._cache_lock:
            if name in self._site_cache:
                return self._site_cache[name]

        # Check if site file exists
        site_path = self.workspace / "sites" / f"{name}.yaml"
        if not site_path.exists():
            # Also check .yml extension
            site_path = self.workspace / "sites" / f"{name}.yml"
            if not site_path.exists():
                raise FileNotFoundError(f"Site file not found: sites/{name}.yaml")

        # Check if this is a SiteTemplate (cannot be loaded directly)
        if self._is_site_template(site_path):
            raise ValueError(
                f"Cannot load '{name}' as a site: it is a SiteTemplate (inheritance-only). "
                f"SiteTemplates cannot be deployed directly."
            )

        # Load and merge site data (handles inheritance + local overlay)
        merged_data = self._load_site_data(name)

        # Validate merged data
        _validate_resource(merged_data, "Site", site_path)

        # Parse merged data (similar to Site.from_file but from dict)
        if "spec" in merged_data:
            spec = merged_data["spec"]
            metadata = merged_data.get("metadata", {})
            site_name = metadata.get("name", name)
            labels = metadata.get("labels", {})
        else:
            spec = merged_data
            site_name = merged_data.get("name", name)
            labels = merged_data.get("labels", {})

        required = ["subscription", "location"]
        for req in required:
            if req not in spec:
                raise ValueError(f"Missing required field '{req}' in site: {name}")

        site = Site(
            name=site_name,
            subscription=spec["subscription"],
            resource_group=spec.get("resourceGroup", ""),
            location=spec["location"],
            labels=labels,
            properties=spec.get("properties", {}),
            parameters=spec.get("parameters", {}),
        )

        # Cache the resolved site
        with self._cache_lock:
            self._site_cache[name] = site

        return site

    def _get_all_site_names(self) -> list[str]:
        """Get all deployable site names from the sites directory.

        Scans the workspace's sites/ directory for YAML files and returns
        the names of files that represent deployable sites (kind: Site).
        Files with kind: SiteTemplate are excluded as they are only used
        for inheritance.

        Returns:
            List of site names (filename stems without .yaml extension)

        Note:
            - Files that cannot be parsed are included and will error during load_site()
            - This allows proper error reporting with full context
        """
        sites_dir = self.workspace / "sites"
        if not sites_dir.exists():
            return []

        site_names = set()  # Use set to avoid duplicates if both .yaml and .yml exist
        for ext in ("*.yaml", "*.yml"):
            for path in sites_dir.glob(ext):
                if self._is_site_template(path):
                    continue
                site_names.add(path.stem)

        return sorted(site_names)  # Sort for deterministic order

    def _is_site_template(self, path: Path) -> bool:
        """Check if a YAML file is a SiteTemplate (inheritance-only).

        Args:
            path: Path to the YAML file

        Returns:
            True if the file has kind: SiteTemplate, False otherwise

        Note:
            Returns False if the file cannot be parsed, allowing load_site()
            to handle the error with proper context.
        """
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            return bool(data and data.get("kind") == "SiteTemplate")
        except (yaml.YAMLError, OSError):
            # Let load_site() handle parsing errors with full context
            return False

    def load_all_sites(self) -> list[Site]:
        """Load all sites from sites/ and sites.local/ directories.

        Sites are merged across directories with the following precedence:
        sites.local/ > sites/

        Returns:
            List of all Site instances found (with merged configuration)
        """
        sites = []
        skipped = []

        for name in self._get_all_site_names():
            try:
                site = self.load_site(name)
                sites.append(site)
            except (ValueError, yaml.YAMLError, OSError) as e:
                logger.warning(f"Failed to load site '{name}': {e}")
                skipped.append((name, str(e)))

        if skipped:
            import sys

            print(f"\n\u26a0 Skipped {len(skipped)} site(s) due to errors:", file=sys.stderr)
            for name, error in skipped:
                print(f"  \u2022 {name}: {error}", file=sys.stderr)
            print(file=sys.stderr)

        return sites

    def load_parameters(self, path: Path) -> dict[str, Any]:
        """Load parameters from a YAML/JSON file with caching.

        Thread-safe caching prevents re-reading files during parallel deployments.
        Returns a deep copy to prevent mutation of cached data.

        Args:
            path: Path to the parameter file

        Returns:
            Dict of parameters (deep copy from cache)
        """
        path = path.resolve()

        with self._params_cache_lock:
            if path in self._params_cache:
                return copy.deepcopy(self._params_cache[path])

        if not path.exists():
            logger.warning(f"Parameter file not found: {path}")
            return {}

        with open(path, "r", encoding="utf-8") as f:
            if path.suffix == ".json":
                result = json.load(f)
            else:
                result = yaml.safe_load(f) or {}

        with self._params_cache_lock:
            self._params_cache[path] = result

        return copy.deepcopy(result)

    def _resolve_template_strings(
        self, value: Any, site: Site, step_outputs: dict[str, dict[str, Any]] | None = None
    ) -> Any:
        """Recursively resolve {{ site.X }} templates in values.

        Supports:
        - {{ site.name }}
        - {{ site.location }}
        - {{ site.resourceGroup }}
        - {{ site.subscription }}
        - {{ site.labels.<key> }}
        - {{ site.properties.<path> }} (nested paths supported)
        - {{ site.parameters.<path> }} (nested paths supported)

        Args:
            value: Value to resolve (string, dict, list, or other)
            site: Site to resolve variables from
            step_outputs: Optional step outputs for chaining

        Returns:
            Value with all site templates resolved
        """
        if isinstance(value, str):
            # Simple replacements
            result = value
            result = result.replace("{{ site.name }}", site.name)
            result = result.replace("{{ site.location }}", site.location)
            result = result.replace("{{ site.resourceGroup }}", site.resource_group)
            result = result.replace("{{ site.subscription }}", site.subscription)

            # Labels
            for key, val in site.labels.items():
                result = result.replace(f"{{{{ site.labels.{key} }}}}", str(val))

            # Properties (complex paths) - may return non-string for entire object/array templates
            result = self._resolve_properties_templates(result, site.properties)

            # Parameters (complex paths) - only if result is still a string
            # (properties resolution may have returned a list/dict for templates like {{ site.properties.endpoints }})
            if isinstance(result, str):
                result = self._resolve_parameters_templates(result, site.parameters)

            return result

        elif isinstance(value, dict):
            return {k: self._resolve_template_strings(v, site, step_outputs) for k, v in value.items()}
        elif isinstance(value, list):
            return [self._resolve_template_strings(v, site, step_outputs) for v in value]
        return value

    def _resolve_parameters_templates(self, value: str, parameters: dict[str, Any]) -> Any:
        """Resolve {{ site.parameters.<path> }} templates in a string.

        Supports nested paths like:
        - {{ site.parameters.clusterName }}
        - {{ site.parameters.brokerConfig.memoryProfile }}

        Args:
            value: String potentially containing parameter templates
            parameters: Site parameters dict

        Returns:
            Resolved value (may be non-string if entire value is a single template)
        """
        # Check if entire string is a single template (for complex types)
        stripped = value.strip()
        full_match = SITE_PARAMETERS_PATTERN.fullmatch(stripped)
        if full_match:
            path = full_match.group(1)
            resolved = self._resolve_property_path(parameters, path)
            if resolved is not None:
                return resolved
            # Return original if path not found
            return value

        # For strings with embedded templates, do string substitution
        def replacer(match: re.Match) -> str:
            path = match.group(1)
            resolved = self._resolve_property_path(parameters, path)
            if resolved is not None:
                return str(resolved)
            return match.group(0)  # Return original if not found

        return SITE_PARAMETERS_PATTERN.sub(replacer, value)

    def _resolve_properties_templates(self, value: str, properties: dict[str, Any]) -> Any:
        """Resolve {{ site.properties.<path> }} templates in a string.

        Supports nested paths like:
        - {{ site.properties.mqtt.broker }}
        - {{ site.properties.deviceEndpoints[0].host }}
        - {{ site.properties.deviceEndpoints }} (returns entire list/object)

        Args:
            value: String potentially containing property templates
            properties: Site properties dict

        Returns:
            Resolved value (may be non-string if entire value is a single template)
        """
        # Check if entire string is a single template (for complex types)
        stripped = value.strip()
        full_match = SITE_PROPERTIES_PATTERN.fullmatch(stripped)
        if full_match:
            path = full_match.group(1)
            resolved = self._resolve_property_path(properties, path)
            if resolved is not None:
                return resolved
            return value

        # For strings with embedded templates, do string substitution
        def replacer(match: re.Match) -> str:
            path = match.group(1)
            resolved = self._resolve_property_path(properties, path)
            if resolved is not None:
                # Convert to string for embedded templates
                if isinstance(resolved, (dict, list)):
                    return json.dumps(resolved)
                return str(resolved)
            return match.group(0)  # Return original if not found

        return SITE_PROPERTIES_PATTERN.sub(replacer, value)

    def _resolve_property_path(self, obj: Any, path: str) -> Any:
        """Resolve a dotted path with optional array indices.

        Examples:
            - "mqtt.broker" -> obj["mqtt"]["broker"]
            - "endpoints[0].host" -> obj["endpoints"][0]["host"]
            - "devices[0]" -> obj["devices"][0]

        Args:
            obj: Object to traverse
            path: Dotted path with optional [N] indices

        Returns:
            Resolved value or None if path doesn't exist
        """

        # Split path into segments, handling array notation
        # e.g., "endpoints[0].host" -> ["endpoints", "[0]", "host"]
        segments = re.split(r"\.(?![^\[]*\])", path)

        current = obj
        for segment in segments:
            if current is None:
                return None

            # Check for array index notation: "name[0]" or just "[0]"
            array_match = re.match(r"^([a-zA-Z0-9_]*)\[(\d+)\]$", segment)
            if array_match:
                key = array_match.group(1)
                index = int(array_match.group(2))

                if key:
                    if not isinstance(current, dict) or key not in current:
                        return None
                    current = current[key]

                if not isinstance(current, list) or index >= len(current):
                    return None
                current = current[index]
            else:
                if not isinstance(current, dict) or segment not in current:
                    return None
                current = current[segment]

        return current

    def _resolve_step_outputs(
        self,
        value: Any,
        step_outputs: dict[str, dict[str, Any]],
        subscription_outputs: SubscriptionOutputs | None = None,
        subscription_id: str | None = None,
    ) -> Any:
        """Recursively resolve {{ steps.<name>.outputs.<path> }} templates.

        Supports output chaining between steps, including cross-scope chaining
        where RG-level sites can reference outputs from subscription-scoped steps.

        Resolution order:
        1. Per-site step_outputs (from RG-scoped steps executed for this site)
        2. Subscription outputs (from subscription-scoped steps for this subscription)

        Args:
            value: Value to resolve (string, dict, list, or other)
            step_outputs: Dict mapping step names to their outputs (per-site)
            subscription_outputs: Dict mapping subscription_id -> step_name -> outputs
            subscription_id: Current site's subscription (for cross-scope resolution)

        Returns:
            Value with all step output references resolved
        """
        if isinstance(value, str):
            # Check if entire string is a single template (for complex types like arrays)
            stripped = value.strip()
            full_match = STEP_OUTPUT_PATTERN.fullmatch(stripped)
            if full_match:
                step_name = full_match.group(1)
                output_path = full_match.group(2)

                # Try per-site outputs first, then subscription outputs
                output_value = self._resolve_output_from_sources(
                    step_name, output_path, step_outputs, subscription_outputs, subscription_id
                )
                if output_value is not None:
                    return output_value
                return value

            # For strings with embedded templates, do string substitution
            def replacer(match: re.Match) -> str:
                step_name = match.group(1)
                output_path = match.group(2)

                output_value = self._resolve_output_from_sources(
                    step_name, output_path, step_outputs, subscription_outputs, subscription_id
                )
                if output_value is None:
                    return match.group(0)

                if isinstance(output_value, (list, dict)):
                    logger.warning(f"Cannot embed complex output '{output_path}' in string: {value}")
                    return match.group(0)

                return str(output_value)

            return STEP_OUTPUT_PATTERN.sub(replacer, value)

        elif isinstance(value, dict):
            return {
                k: self._resolve_step_outputs(v, step_outputs, subscription_outputs, subscription_id)
                for k, v in value.items()
            }
        elif isinstance(value, list):
            return [
                self._resolve_step_outputs(item, step_outputs, subscription_outputs, subscription_id) for item in value
            ]
        return value

    @staticmethod
    def _resolve_output_from_sources(
        step_name: str,
        output_path: str,
        step_outputs: dict[str, dict[str, Any]],
        subscription_outputs: SubscriptionOutputs | None,
        subscription_id: str | None,
    ) -> Any:
        """Resolve an output reference from available sources.

        Args:
            step_name: Name of the step to get outputs from
            output_path: Dot-separated path within the outputs
            step_outputs: Per-site step outputs
            subscription_outputs: Subscription-level step outputs
            subscription_id: Current subscription ID

        Returns:
            Resolved value or None if not found
        """
        # Try per-site outputs first
        step_data = step_outputs.get(step_name)
        if step_data is not None:
            output_value = _resolve_output_path(step_data, output_path)
            if output_value is not None:
                return output_value

        # Fall back to subscription outputs
        if subscription_outputs and subscription_id:
            sub_step_data = subscription_outputs.get(subscription_id, {}).get(step_name)
            if sub_step_data is not None:
                return _resolve_output_path(sub_step_data, output_path)

        return None

    def resolve_parameters(
        self,
        step: DeploymentStep,
        site: Site,
        manifest: Manifest,
        step_outputs: dict[str, dict[str, Any]] | None = None,
        subscription_outputs: SubscriptionOutputs | None = None,
    ) -> dict[str, Any]:
        """Merge and resolve parameters for a deployment step.

        Parameter merge order (later overrides earlier):
        1. Manifest-level parameter files (from manifest.parameters) - shared defaults
        2. Site-level parameters (from site definition) - site-specific overrides
        3. Step-level parameter files (from step.parameters) - step-specific overrides

        After merging, parameters are:
        - Resolved with template variable substitution ({{ site.X }}, {{ steps.X.outputs.Y }})
        - Filtered to only include parameters accepted by the template

        Args:
            step: The deployment step
            site: Target site
            manifest: The manifest being deployed
            step_outputs: Outputs from previous steps (for chaining)
            subscription_outputs: Outputs from subscription-scoped steps (for cross-scope chaining)

        Returns:
            Fully resolved and filtered parameters dict
        """
        # 1. Start with manifest-level parameter files (shared defaults)
        params: dict[str, Any] = {}
        for param_path in manifest.parameters:
            resolved_path = manifest.resolve_parameter_path(param_path, site)
            full_path = (self.workspace / resolved_path).resolve()
            if full_path.exists():
                file_params = self.load_parameters(full_path)
                params = self._deep_merge(params, file_params)
            else:
                logger.warning(f"Manifest parameter file not found: {full_path}")

        # 2. Merge site-level parameters (site-specific overrides)
        params = self._deep_merge(params, site.get_all_parameters())

        # 3. Merge step-level parameter files (step-specific overrides)
        for param_path in step.parameters:
            resolved_path = manifest.resolve_parameter_path(param_path, site)
            full_path = (self.workspace / resolved_path).resolve()
            if full_path.exists():
                file_params = self.load_parameters(full_path)
                params = self._deep_merge(params, file_params)
            else:
                logger.warning(f"Step parameter file not found: {full_path}")

        # 4. Resolve template variables ({{ site.X }})
        params = self._resolve_template_strings(params, site)

        # 5. Resolve step output references ({{ steps.X.outputs.Y }})
        # Includes cross-scope resolution from subscription outputs
        if step_outputs or subscription_outputs:
            params = self._resolve_step_outputs(
                params,
                step_outputs or {},
                subscription_outputs,
                site.subscription,
            )

        # 6. Warn about any unresolved templates
        self._check_unresolved_templates(params, site.name)

        # 7. Filter to only parameters accepted by the template
        template_path = (self.workspace / step.template).resolve()
        if template_path.exists():
            try:
                params = filter_parameters(params, str(template_path), step.name)
            except (ValueError, FileNotFoundError) as e:
                logger.warning(f"Could not filter parameters for step '{step.name}': {e}")
                # Continue with unfiltered params - let ARM validate

        return params

    def _check_unresolved_templates(self, params: dict[str, Any], site_name: str) -> None:
        """Warn if any {{ ... }} templates weren't resolved."""

        def check_value(v: Any, path: str = "") -> None:
            if isinstance(v, str) and "{{" in v and "}}" in v:
                logger.warning(f"Unresolved template in {path}: {v} (site: {site_name})")
            elif isinstance(v, dict):
                for k, val in v.items():
                    check_value(val, f"{path}.{k}" if path else k)
            elif isinstance(v, list):
                for i, item in enumerate(v):
                    check_value(item, f"{path}[{i}]")

        check_value(params)

    def _evaluate_condition(self, condition: str | None, site: Site) -> bool:
        """Evaluate a step condition against a site.

        Supports:
        - {{ site.labels.key == 'value' }}
        - {{ site.labels.key != 'value' }}
        - {{ site.properties.path == 'value' }}
        - {{ site.properties.path != 'value' }}
        - {{ site.properties.nested.path == 'value' }}
        - {{ site.properties.array[0].field == 'value' }}
        - {{ site.properties.path == true }}
        - {{ site.properties.path == false }}
        - {{ site.properties.path }} (truthy check)

        Truthy check returns True if:
        - Boolean: value is True
        - String: value is not empty and not in ('false', '0') (case-insensitive)
        - Number: value is not 0
        - List/Dict: value is not empty

        Args:
            condition: The condition expression (or None)
            site: The site to evaluate against

        Returns:
            True if condition passes (or is None/empty), False otherwise
        """
        if not condition:
            return True

        condition = condition.strip()
        match = CONDITION_PATTERN.fullmatch(condition)
        if not match:
            logger.warning(f"Invalid condition syntax: {condition}")
            return True

        field_path = match.group(1)  # e.g., "labels.environment" or "properties.deployOptions.includeSolution"
        operator = match.group(2)  # "==" or "!=" or None (for truthy check)
        # Group 3 is quoted string value, group 4 is unquoted boolean
        expected_value = match.group(3) if match.group(3) is not None else match.group(4)

        # Resolve the actual value based on field path
        if field_path.startswith("labels."):
            label_key = field_path[7:]  # Remove "labels." prefix
            actual_value = site.labels.get(label_key, "")
            raw_value = actual_value  # For truthy check
        elif field_path.startswith("properties."):
            prop_path = field_path[11:]  # Remove "properties." prefix
            raw_value = self._resolve_property_path(site.properties, prop_path)
            # Convert to string for comparison (booleans become "true"/"false")
            if raw_value is None:
                actual_value = ""
            elif isinstance(raw_value, bool):
                actual_value = "true" if raw_value else "false"
            else:
                actual_value = str(raw_value)
        else:
            logger.warning(f"Unknown condition field type: {field_path}")
            return True

        # Handle truthy check (no operator)
        if operator is None:
            # Truthy: True for bool True, non-empty strings, non-zero numbers
            if raw_value is None:
                return False
            if isinstance(raw_value, bool):
                return raw_value
            if isinstance(raw_value, str):
                return raw_value.lower() not in ("", "false", "0")
            if isinstance(raw_value, (int, float)):
                return raw_value != 0
            # For lists/dicts, truthy if non-empty
            return bool(raw_value)

        # Handle comparison operators
        if operator == "==":
            return actual_value == expected_value
        elif operator == "!=":
            return actual_value != expected_value

        return True

    @staticmethod
    def _check_step_site_compatibility(step: ManifestStep, site: Site) -> str | None:
        """Check if a step should run for a given site based on scope compatibility.

        Args:
            step: The manifest step to check
            site: The site to check against

        Returns:
            Skip reason string if incompatible, None if compatible
        """
        # Kubectl steps run on any site with a cluster
        if isinstance(step, KubectlStep):
            return None

        # Check scope/site level compatibility
        is_sub_level = site.is_subscription_level
        if step.scope == "subscription" and not is_sub_level:
            return "subscription-scoped step, site has resource group"
        if step.scope == "resourceGroup" and is_sub_level:
            return "resourceGroup-scoped step, site has no resource group"

        return None

    @staticmethod
    def _get_step_type_label(step: ManifestStep) -> str:
        """Get a display label for the step type.

        Args:
            step: The manifest step

        Returns:
            Display string like 'resourceGroup', 'subscription', or 'kubectl:apply'
        """
        if isinstance(step, KubectlStep):
            return f"kubectl:{step.operation}"
        return step.scope

    @staticmethod
    def _get_subscription_step_names(manifest: Manifest) -> set[str]:
        """Get names of all subscription-scoped steps in a manifest.

        Args:
            manifest: The manifest to inspect

        Returns:
            Set of step names that have scope: subscription
        """
        return {
            step.name for step in manifest.steps if isinstance(step, DeploymentStep) and step.scope == "subscription"
        }

    def _any_subscription_step_would_execute(
        self,
        subscription_steps: list[DeploymentStep],
        rg_level_sites: list[Site],
    ) -> bool:
        """Check if any subscription-scoped step would execute for any RG-level site.

        Used during validation to determine if a subscription-level site is actually
        needed. If all subscription-scoped steps have `when` conditions that evaluate
        to False for all RG-level sites, no subscription-level site is required.

        Args:
            subscription_steps: List of subscription-scoped steps to check
            rg_level_sites: RG-level sites in the subscription

        Returns:
            True if at least one step would execute (needs subscription-level site)
        """
        for step in subscription_steps:
            # No condition = always runs
            if not step.when:
                return True

            # Check if condition passes for any RG-level site
            for site in rg_level_sites:
                if self._evaluate_condition(step.when, site):
                    return True

        return False

    @staticmethod
    def _references_any_step(value: Any, step_names: set[str]) -> bool:
        """Check if a value contains output references to any of the given steps.

        Recursively searches dict/list/str for {{ steps.<name>.outputs.* }} patterns.

        Args:
            value: Parameter value to check
            step_names: Set of step names to look for

        Returns:
            True if value references any step in step_names
        """
        if isinstance(value, dict):
            return any(Orchestrator._references_any_step(v, step_names) for v in value.values())
        elif isinstance(value, list):
            return any(Orchestrator._references_any_step(item, step_names) for item in value)
        elif isinstance(value, str):
            # Quick check before regex
            if "steps." not in value:
                return False
            for match in STEP_OUTPUT_PATTERN.finditer(value):
                if match.group(1) in step_names:
                    return True
        return False

    def _site_depends_on_subscription_outputs(
        self,
        manifest: Manifest,
        site: Site,
        subscription_step_names: set[str],
    ) -> bool:
        """Check if a site's RG-scoped steps reference subscription-scoped outputs.

        Scans manifest-level and step-level parameter files for references to
        subscription-scoped step outputs.

        Args:
            manifest: The manifest being deployed
            site: The site to check
            subscription_step_names: Names of subscription-scoped steps

        Returns:
            True if site has steps that depend on subscription-scoped outputs
        """
        if not subscription_step_names:
            return False

        # Check manifest-level parameters (apply to all steps)
        for param_path in manifest.parameters:
            resolved_path = manifest.resolve_parameter_path(param_path, site)
            full_path = (self.workspace / resolved_path).resolve()
            if full_path.exists():
                try:
                    params = self.load_parameters(full_path)
                    if self._references_any_step(params, subscription_step_names):
                        return True
                except (ValueError, yaml.YAMLError, OSError) as e:
                    logger.debug(f"Could not read parameter file {full_path}: {e}")

        # Check step-level parameters for RG-scoped steps
        for step in manifest.steps:
            if isinstance(step, DeploymentStep) and step.scope == "resourceGroup":
                for param_path in step.parameters:
                    resolved_path = manifest.resolve_parameter_path(param_path, site)
                    full_path = (self.workspace / resolved_path).resolve()
                    if full_path.exists():
                        try:
                            params = self.load_parameters(full_path)
                            if self._references_any_step(params, subscription_step_names):
                                return True
                        except (ValueError, yaml.YAMLError, OSError) as e:
                            logger.debug(f"Could not read parameter file {full_path}: {e}")

        return False

    def _deploy_bicep_step(
        self,
        site: Site,
        step: DeploymentStep,
        manifest: Manifest,
        timestamp: str,
        step_outputs: dict[str, dict[str, Any]],
        subscription_outputs: SubscriptionOutputs | None = None,
    ) -> DeploymentResult:
        """Execute a Bicep/ARM deployment step.

        Args:
            site: Target site
            step: The deployment step
            manifest: The manifest being deployed
            timestamp: Shared timestamp for deployment naming
            step_outputs: Outputs from previous steps (per-site)
            subscription_outputs: Outputs from subscription-scoped steps (for cross-scope chaining)

        Returns:
            DeploymentResult with success status and outputs
        """
        params = self.resolve_parameters(step, site, manifest, step_outputs, subscription_outputs)
        template_path = (self.workspace / step.template).resolve()

        # Azure deployment names have a 64 char limit
        # Format: {base_name}-{timestamp} where timestamp is 14 chars (YYYYMMDDHHmmss)
        base_name = f"{manifest.name}-{site.name}-{step.name}"
        MAX_LEN = 64
        TIMESTAMP_LEN = 14
        max_base = MAX_LEN - TIMESTAMP_LEN - 1  # -1 for the separator

        if len(base_name) > max_base:
            # Use hash suffix to ensure uniqueness when truncating
            name_hash = hashlib.md5(base_name.encode()).hexdigest()[:6]
            base_name = f"{base_name[:max_base - 7]}-{name_hash}"

        deployment_name = f"{base_name}-{timestamp}"

        if step.scope == "subscription":
            return self.executor.deploy_subscription(
                subscription=site.subscription,
                location=site.location,
                template_path=template_path,
                parameters=params,
                deployment_name=deployment_name,
                step_name=step.name,
                site_name=site.name,
            )
        else:
            return self.executor.deploy_resource_group(
                subscription=site.subscription,
                resource_group=site.resource_group,
                template_path=template_path,
                parameters=params,
                deployment_name=deployment_name,
                step_name=step.name,
                site_name=site.name,
            )

    def _execute_kubectl_step(
        self,
        site: Site,
        step: KubectlStep,
        step_outputs: dict[str, dict[str, Any]],
        subscription_outputs: SubscriptionOutputs | None = None,
    ) -> KubectlResult:
        """Execute a kubectl step against an Arc-connected cluster.

        Args:
            site: Target site
            step: The kubectl step
            step_outputs: Outputs from previous steps (per-site)
            subscription_outputs: Outputs from subscription-scoped steps

        Returns:
            KubectlResult with success status
        """
        # Resolve template variables in Arc config
        cluster_name = self._resolve_template_strings(step.arc.name, site)
        resource_group = self._resolve_template_strings(step.arc.resource_group, site)

        if step_outputs or subscription_outputs:
            cluster_name = self._resolve_step_outputs(
                cluster_name, step_outputs, subscription_outputs, site.subscription
            )
            resource_group = self._resolve_step_outputs(
                resource_group, step_outputs, subscription_outputs, site.subscription
            )

        # Resolve template variables in files list
        resolved_files = []
        for f in step.files:
            resolved = self._resolve_template_strings(f, site)
            if step_outputs or subscription_outputs:
                resolved = self._resolve_step_outputs(resolved, step_outputs, subscription_outputs, site.subscription)
            resolved_files.append(resolved)

        if step.operation == "apply":
            return self.executor.kubectl_apply(
                cluster_name=cluster_name,
                resource_group=resource_group,
                subscription=site.subscription,
                files=resolved_files,
                step_name=step.name,
                site_name=site.name,
            )
        else:
            # Should not happen due to model validation
            return KubectlResult(
                success=False,
                step_name=step.name,
                site_name=site.name,
                error=f"Unsupported kubectl operation: {step.operation}",
            )

    def _execute_step(
        self,
        site: Site,
        step: ManifestStep,
        manifest: Manifest,
        timestamp: str,
        step_outputs: dict[str, dict[str, Any]],
        subscription_outputs: SubscriptionOutputs | None = None,
    ) -> StepResult:
        """Execute a single step (deployment or kubectl).

        Args:
            site: Target site
            step: The step to execute
            manifest: The manifest being deployed
            timestamp: Shared timestamp for deployment naming
            step_outputs: Outputs from previous steps (per-site)
            subscription_outputs: Outputs from subscription-scoped steps

        Returns:
            StepResult (DeploymentResult or KubectlResult)
        """
        if isinstance(step, KubectlStep):
            return self._execute_kubectl_step(site, step, step_outputs, subscription_outputs)
        else:
            return self._deploy_bicep_step(site, step, manifest, timestamp, step_outputs, subscription_outputs)

    def _deploy_site(
        self,
        manifest: Manifest,
        site: Site,
        timestamp: str,
        parallel_mode: bool = False,
        subscription_outputs: SubscriptionOutputs | None = None,
    ) -> dict[str, Any]:
        """Deploy all applicable steps to a single site.

        Steps are executed sequentially. If a step fails, remaining steps
        are skipped for that site.

        Step applicability based on site type:
        - Subscription-level sites: Only execute subscription-scoped steps
        - RG-level sites: Only execute RG-scoped steps (can reference subscription outputs)

        Args:
            manifest: The manifest being deployed
            site: Target site
            timestamp: Shared timestamp for deployment naming
            parallel_mode: If True, use thread-safe printing
            subscription_outputs: Outputs from subscription-scoped steps (for cross-scope chaining)

        Returns:
            Dict with site deployment result including status, steps, and timing
        """
        site_start = time.time()
        step_outputs: dict[str, dict[str, Any]] = {}
        log = _thread_safe_print if parallel_mode else print

        steps_completed = 0
        steps_skipped = 0
        status = "success"
        error_message: str | None = None
        step_results: list[dict[str, Any]] = []

        for step in manifest.steps:
            # Check step/site scope compatibility
            skip_reason = self._check_step_site_compatibility(step, site)
            if skip_reason:
                log(f"[{site.name}] - {step.name} (skipped: {skip_reason})")
                steps_skipped += 1
                step_results.append(
                    {
                        "step": step.name,
                        "status": "skipped",
                        "reason": skip_reason,
                    }
                )
                continue

            # Evaluate condition
            if not self._evaluate_condition(step.when, site):
                log(f"[{site.name}] - {step.name} (skipped: condition not met)")
                steps_skipped += 1
                step_results.append(
                    {
                        "step": step.name,
                        "status": "skipped",
                        "reason": f"Condition not met: {step.when}",
                    }
                )
                continue

            step_type = self._get_step_type_label(step)
            log(f"[{site.name}] > {step.name} ({step_type})...")

            result = self._execute_step(site, step, manifest, timestamp, step_outputs, subscription_outputs)

            if result.success:
                # Only DeploymentResult has outputs for chaining
                outputs = result.outputs or {} if isinstance(result, DeploymentResult) else {}
                if outputs:
                    step_outputs[step.name] = outputs
                log(f"[{site.name}] + {step.name}")
                steps_completed += 1
                step_results.append(
                    {
                        "step": step.name,
                        "status": "success",
                        "outputs": outputs,
                    }
                )
            else:
                log(f"[{site.name}] x {step.name}: {result.error}")
                status = "failed"
                error_message = result.error
                step_results.append(
                    {
                        "step": step.name,
                        "status": "failed",
                        "error": result.error,
                    }
                )
                break

        elapsed = time.time() - site_start
        total_steps = len(manifest.steps)

        skip_info = f", {steps_skipped} skipped" if steps_skipped > 0 else ""
        status_symbol = "+" if status == "success" else "x"
        log(
            f"[{site.name}] {status_symbol} completed in {elapsed:.1f}s "
            f"({steps_completed}/{total_steps - steps_skipped} steps{skip_info})"
        )

        return {
            "site": site.name,
            "status": status,
            "error": error_message,
            "steps_completed": steps_completed,
            "steps_skipped": steps_skipped,
            "steps_total": total_steps,
            "elapsed": elapsed,
            "steps": step_results,
        }

    def _deploy_sequential(
        self,
        manifest: Manifest,
        sites: list[Site],
        timestamp: str,
        subscription_outputs: SubscriptionOutputs | None = None,
    ) -> list[dict[str, Any]]:
        """Deploy to sites sequentially (one at a time).

        Args:
            manifest: The manifest being deployed
            sites: List of target sites
            timestamp: Shared timestamp for deployment naming
            subscription_outputs: Outputs from subscription-scoped steps (for RG-scoped steps)

        Returns:
            List of deployment results per site
        """
        results: list[dict[str, Any]] = []
        for site in sites:
            result = self._deploy_site(
                manifest,
                site,
                timestamp,
                parallel_mode=False,
                subscription_outputs=subscription_outputs,
            )
            results.append(result)
        return results

    def _deploy_parallel(
        self,
        manifest: Manifest,
        sites: list[Site],
        timestamp: str,
        parallel_config: ParallelConfig,
        subscription_outputs: SubscriptionOutputs | None = None,
    ) -> list[dict[str, Any]]:
        """Deploy to sites in parallel with controlled concurrency.

        Args:
            manifest: The manifest being deployed
            sites: List of target sites
            timestamp: Shared timestamp for deployment naming
            parallel_config: Parallelism configuration
            subscription_outputs: Outputs from subscription-scoped steps (for RG-scoped steps)

        Returns:
            List of deployment results per site
        """
        max_workers = parallel_config.max_workers
        # If unlimited (None), cap at number of sites
        num_workers = len(sites) if max_workers is None else min(len(sites), max_workers)

        print(f"\n  [Parallel] Deploying to {len(sites)} sites ({num_workers} concurrent)")

        results: list[dict[str, Any]] = []
        results_lock = threading.Lock()

        with ThreadPoolExecutor(max_workers=num_workers) as executor:
            future_to_site = {
                executor.submit(self._deploy_site, manifest, site, timestamp, True, subscription_outputs): site
                for site in sites
            }

            for future in as_completed(future_to_site):
                site = future_to_site[future]
                try:
                    result = future.result()
                    with results_lock:
                        results.append(result)
                except Exception as e:
                    logger.error(f"Unexpected error deploying to {site.name}: {e}")
                    with results_lock:
                        results.append(
                            {
                                "site": site.name,
                                "status": "failed",
                                "error": f"Unexpected error: {e}",
                                "steps_completed": 0,
                                "steps_skipped": 0,
                                "steps_total": len(manifest.steps),
                                "elapsed": 0.0,
                                "steps": [],
                            }
                        )

        return results

    @staticmethod
    def _group_sites_by_subscription(
        sites: list[Site],
    ) -> dict[str, tuple[list[Site], list[Site]]]:
        """Group sites by subscription ID, separating subscription-level from RG-level.

        Args:
            sites: List of sites to group

        Returns:
            Dict mapping subscription_id to (subscription_sites, rg_sites) tuple
        """
        groups: dict[str, tuple[list[Site], list[Site]]] = {}

        for site in sites:
            sub_id = site.subscription
            if sub_id not in groups:
                groups[sub_id] = ([], [])

            sub_sites, rg_sites = groups[sub_id]
            if site.is_subscription_level:
                sub_sites.append(site)
            else:
                rg_sites.append(site)

        return groups

    @staticmethod
    def _has_subscription_scoped_steps(manifest: Manifest) -> bool:
        """Check if manifest has any subscription-scoped steps.

        Args:
            manifest: The manifest to check

        Returns:
            True if any step has scope: subscription
        """
        for step in manifest.steps:
            if isinstance(step, DeploymentStep) and step.scope == "subscription":
                return True
        return False

    def _collect_subscription_outputs(
        self,
        manifest: Manifest,
        subscription_sites: dict[str, Site],
        timestamp: str,
        parallel_config: ParallelConfig,
    ) -> tuple[SubscriptionOutputs, list[dict[str, Any]]]:
        """Execute subscription-scoped steps and collect outputs.

        Args:
            manifest: The manifest being deployed
            subscription_sites: Dict mapping subscription_id to subscription-level site
            timestamp: Shared timestamp for deployment naming
            parallel_config: Parallelism configuration

        Returns:
            Tuple of (subscription_outputs, results)
        """
        subscription_outputs: SubscriptionOutputs = {}
        results: list[dict[str, Any]] = []

        # Get subscription-level sites as a list
        sub_level_sites = list(subscription_sites.values())

        if not sub_level_sites:
            return subscription_outputs, results

        print(f"\n  [Phase 1] Subscription-scoped steps: {len(subscription_sites)} subscription(s)")

        # Deploy to subscription-level sites (they'll skip RG-scoped steps)
        if parallel_config.is_sequential or len(sub_level_sites) == 1:
            for site in sub_level_sites:
                result = self._deploy_site(
                    manifest,
                    site,
                    timestamp,
                    parallel_mode=False,
                    subscription_outputs=subscription_outputs,
                )
                results.append(result)
                # Collect outputs into subscription_outputs keyed by subscription
                self._extract_subscription_outputs(result, site.subscription, subscription_outputs)
        else:
            # Parallel deployment across subscriptions
            max_workers = parallel_config.max_workers
            num_workers = len(sub_level_sites) if max_workers is None else min(len(sub_level_sites), max_workers)

            with ThreadPoolExecutor(max_workers=num_workers) as executor:
                future_to_site = {
                    executor.submit(self._deploy_site, manifest, site, timestamp, True, subscription_outputs): site
                    for site in sub_level_sites
                }

                for future in as_completed(future_to_site):
                    site = future_to_site[future]
                    try:
                        result = future.result()
                        results.append(result)
                        self._extract_subscription_outputs(result, site.subscription, subscription_outputs)
                    except Exception as e:
                        logger.error(f"Error deploying subscription-level site {site.name}: {e}")
                        results.append(
                            {
                                "site": site.name,
                                "status": "failed",
                                "error": str(e),
                                "steps_completed": 0,
                                "steps_skipped": 0,
                                "steps_total": len(manifest.steps),
                                "elapsed": 0.0,
                                "steps": [],
                            }
                        )

        return subscription_outputs, results

    @staticmethod
    def _extract_subscription_outputs(
        result: dict[str, Any],
        subscription_id: str,
        subscription_outputs: SubscriptionOutputs,
    ) -> None:
        """Extract step outputs from a deployment result into subscription_outputs.

        Args:
            result: Deployment result from _deploy_site
            subscription_id: The subscription ID to key outputs by
            subscription_outputs: Dict to populate (mutated in place)
        """
        sub_outputs = subscription_outputs.setdefault(subscription_id, {})
        for step_result in result.get("steps", []):
            outputs = step_result.get("outputs")
            if step_result.get("status") == "success" and outputs:
                sub_outputs[step_result["step"]] = outputs

    def _print_deployment_summary(
        self,
        results: list[dict[str, Any]],
        total_elapsed: float,
    ) -> None:
        """Print deployment summary.

        Args:
            results: List of deployment results per site
            total_elapsed: Total elapsed time in seconds
        """
        succeeded = sum(1 for r in results if r["status"] == "success")
        failed = sum(1 for r in results if r["status"] == "failed")
        blocked = sum(1 for r in results if r["status"] == "blocked")
        total = len(results)

        print()
        print("=" * 60)
        print("  Deployment Summary")
        print("=" * 60)
        print()

        # Results table header
        print(f"  {'SITE':<25} {'STATUS':<10} {'STEPS':<15} {'DURATION':<10}")
        print(f"  {'-'*25} {'-'*10} {'-'*15} {'-'*10}")

        # Sort by site name for consistent output
        for result in sorted(results, key=lambda r: r["site"]):
            site = result["site"]
            result_status = result["status"]
            if result_status == "success":
                status = "+ Success"
            elif result_status == "blocked":
                status = "- Blocked"
            else:
                status = "x Failed"
            steps = f"{result['steps_completed']}/{result['steps_total']}"
            if result.get("steps_skipped"):
                steps += f" ({result['steps_skipped']} skip)"
            duration = f"{result['elapsed']:.1f}s"

            print(f"  {site:<25} {status:<10} {steps:<15} {duration:<10}")

        print()
        summary_parts = [f"{succeeded} succeeded", f"{failed} failed"]
        if blocked:
            summary_parts.append(f"{blocked} blocked")
        print(f"  Total: {', '.join(summary_parts)} ({total} sites)")
        print(f"  Duration: {total_elapsed:.1f}s")
        print()

        # Show errors for failed sites
        failed_results = [r for r in results if r["status"] == "failed"]
        if failed_results:
            print("  Failed Sites:")
            for result in failed_results:
                error = result.get("error", "Unknown error")
                print(f"    [{result['site']}] {error}")
            print()

        # Show blocked sites
        blocked_results = [r for r in results if r["status"] == "blocked"]
        if blocked_results:
            print("  Blocked Sites:")
            for result in blocked_results:
                error = result.get("error", "Blocked due to subscription failure")
                print(f"    [{result['site']}] {error}")
            print()

    def resolve_sites(self, manifest: Manifest, cli_selector: str | None = None) -> list[Site]:
        """Resolve sites from manifest, applying selectors.

        Priority:
        1. CLI --selector overrides everything
        2. Explicit sites list in manifest
        3. Manifest siteSelector

        Args:
            manifest: The manifest
            cli_selector: Optional selector from CLI

        Returns:
            List of matching sites
        """
        # CLI selector requires loading all sites for filtering
        if cli_selector:
            all_sites = self.load_all_sites()
            selector = parse_selector(cli_selector)
            return [s for s in all_sites if s.matches_selector(selector)]

        # Explicit sites list - load only the named sites (most common case)
        if manifest.sites:
            missing = []
            sites = []
            for name in manifest.sites:
                try:
                    sites.append(self.load_site(name))
                except FileNotFoundError:
                    missing.append(name)
            if missing:
                names = ", ".join(missing)
                raise FileNotFoundError(
                    f"Site files not found for manifest '{manifest.name}': {names}. "
                    f"Check that matching YAML files exist in the sites/ directory."
                )
            return sites

        # Site selector requires loading all sites for filtering
        if manifest.site_selector:
            all_sites = self.load_all_sites()
            selector = parse_selector(manifest.site_selector)
            return [s for s in all_sites if s.matches_selector(selector)]

        return []

    def validate(self, manifest_path: Path, selector: str | None = None) -> list[str]:
        """Validate manifest and return list of errors.

        Checks:
        - Manifest parses correctly
        - Sites exist and match criteria
        - Template files exist
        - Parameter files exist and are valid YAML (manifest and step level)
        - Kubectl files exist (for local files) and use HTTPS
        - Conditions have valid syntax
        - Required site fields are present
        - Step output references point to valid prior steps (accounting for auto-filtering)

        Args:
            manifest_path: Path to manifest file
            selector: Optional site selector

        Returns:
            List of error messages (empty if valid)
        """
        errors: list[str] = []

        try:
            manifest = Manifest.from_file(manifest_path)
        except Exception as e:
            return [f"Failed to parse manifest: {e}"]

        sites = self.resolve_sites(manifest, selector)
        if not sites:
            if manifest.sites or manifest.site_selector or selector:
                errors.append("No sites matched the specified criteria")

        # Validate manifest-level parameter files
        for param_path in manifest.parameters:
            if "{{" in param_path:
                # Dynamic path — validate resolved path for each site
                for site in sites:
                    resolved = manifest.resolve_parameter_path(param_path, site)
                    full_path = (self.workspace / resolved).resolve()
                    if not full_path.exists():
                        errors.append(
                            f"Manifest parameter file not found: {resolved} "
                            f"(resolved from '{param_path}' for site '{site.name}')"
                        )
                    else:
                        try:
                            self.load_parameters(full_path)
                        except Exception as e:
                            errors.append(f"Invalid manifest parameter file {resolved}: {e}")
            else:
                full_path = (self.workspace / param_path).resolve()
                if not full_path.exists():
                    errors.append(f"Manifest parameter file not found: {param_path}")
                else:
                    try:
                        self.load_parameters(full_path)
                    except Exception as e:
                        errors.append(f"Invalid manifest parameter file {param_path}: {e}")

        # Build step name lookup for output reference validation
        all_step_names = {step.name for step in manifest.steps}

        # Check for duplicate step names
        seen_names: set[str] = set()
        for step in manifest.steps:
            if step.name in seen_names:
                errors.append(f"Duplicate step name: '{step.name}'")
            seen_names.add(step.name)

        for step_index, step in enumerate(manifest.steps):
            # Steps that execute before this one (valid sources for output references)
            prior_step_names = {s.name for s in manifest.steps[:step_index]}

            if isinstance(step, KubectlStep):
                # Validate kubectl files (skip URLs and templates)
                for file_path in step.files:
                    if file_path.startswith("https://") or "{{" in file_path:
                        continue
                    if file_path.lower().startswith("http://"):
                        errors.append(f"HTTP URLs not allowed (use HTTPS): {file_path} (step: {step.name})")
                        continue
                    full_path = (self.workspace / file_path).resolve()
                    if not full_path.exists():
                        errors.append(f"Kubectl file not found: {file_path} (step: {step.name})")
            else:
                template_path = (self.workspace / step.template).resolve()

                if not template_path.exists():
                    errors.append(f"Template not found: {step.template}")
                    continue

                for param_path in step.parameters:
                    if "{{" in param_path:
                        # Dynamic path — validate resolved path for each site
                        for site in sites:
                            resolved = manifest.resolve_parameter_path(param_path, site)
                            full_path = (self.workspace / resolved).resolve()
                            if not full_path.exists():
                                errors.append(
                                    f"Parameter file not found: {resolved} "
                                    f"(step: {step.name}, resolved from '{param_path}' for site '{site.name}')"
                                )
                            else:
                                try:
                                    params = self.load_parameters(full_path)
                                    errors.extend(
                                        self._validate_output_references(
                                            params,
                                            step.name,
                                            prior_step_names,
                                            all_step_names,
                                            resolved,
                                            None,
                                        )
                                    )
                                except Exception as e:
                                    errors.append(f"Invalid parameter file {resolved}: {e}")
                        continue

                    full_path = (self.workspace / param_path).resolve()
                    if not full_path.exists():
                        errors.append(f"Parameter file not found: {param_path} (step: {step.name})")
                    else:
                        try:
                            params = self.load_parameters(full_path)

                            # Check if params contain self-references before expensive template parsing
                            has_self_ref = self._contains_self_reference(params, step.name)

                            template_params: frozenset | None = None
                            if has_self_ref:
                                # Only extract template params when needed for self-reference validation
                                try:
                                    from siteops.executor import get_template_parameters

                                    template_params = frozenset(get_template_parameters(str(template_path)))
                                except Exception as e:
                                    logger.debug(f"Could not extract template params for '{step.name}': {e}")
                                    # Continue without template params - validation will be conservative

                            # Validate step output references with auto-filter awareness
                            errors.extend(
                                self._validate_output_references(
                                    params,
                                    step.name,
                                    prior_step_names,
                                    all_step_names,
                                    param_path,
                                    template_params,
                                )
                            )
                        except Exception as e:
                            errors.append(f"Invalid parameter file {param_path}: {e}")

        if not manifest.steps:
            errors.append("Manifest has no steps defined")

        for step in manifest.steps:
            if step.when:
                if not CONDITION_PATTERN.fullmatch(step.when.strip()):
                    errors.append(f"Invalid 'when' condition in step '{step.name}': {step.when}")

        for step in manifest.steps:
            if isinstance(step, DeploymentStep) and step.scope == "resourceGroup":
                for site in sites:
                    # Subscription-level sites are exempt - they intentionally skip RG-scoped steps
                    if site.is_subscription_level:
                        continue
                    if not site.resource_group:
                        errors.append(f"Site '{site.name}' missing 'resourceGroup' required by step '{step.name}'")

        # Validate subscription-scoped steps
        subscription_steps = [
            step for step in manifest.steps if isinstance(step, DeploymentStep) and step.scope == "subscription"
        ]

        if subscription_steps and sites:
            # Group sites by subscription to check for subscription-level sites
            site_groups = self._group_sites_by_subscription(sites)

            # Check that each subscription has exactly one subscription-level site
            for sub_id, (sub_level_sites, rg_level_sites) in site_groups.items():
                if not sub_level_sites and rg_level_sites:
                    # RG-level sites exist but no subscription-level site.
                    # Check if any subscription-scoped step would actually execute
                    # based on its `when` condition evaluated against RG-level sites.
                    needs_subscription_site = self._any_subscription_step_would_execute(
                        subscription_steps, rg_level_sites
                    )

                    if needs_subscription_site:
                        site_names = ", ".join(s.name for s in rg_level_sites[:3])
                        if len(rg_level_sites) > 3:
                            site_names += f"... and {len(rg_level_sites) - 3} more"
                        errors.append(
                            f"Subscription '{sub_id[:8]}...' has RG-level sites ({site_names}) "
                            f"but no subscription-level site for subscription-scoped steps"
                        )
                elif len(sub_level_sites) > 1:
                    # Multiple subscription-level sites for same subscription
                    site_names = ", ".join(s.name for s in sub_level_sites)
                    errors.append(
                        f"Subscription '{sub_id[:8]}...' has multiple subscription-level sites: {site_names}. "
                        f"Only one subscription-level site per subscription is allowed."
                    )

        return errors

    def _contains_self_reference(self, value: Any, step_name: str) -> bool:
        """Check if a value contains a self-reference to the given step.

        This is a quick check to avoid expensive template parameter extraction
        when there are no self-references to validate.

        Args:
            value: Parameter value to check (recursively handles dict/list/str)
            step_name: Name of the current step

        Returns:
            True if value contains {{ steps.<step_name>.outputs... }}
        """
        if isinstance(value, dict):
            return any(self._contains_self_reference(v, step_name) for v in value.values())
        elif isinstance(value, list):
            return any(self._contains_self_reference(item, step_name) for item in value)
        elif isinstance(value, str):
            # Quick string check before regex
            pattern = f"steps.{step_name}."
            if pattern not in value:
                return False
            for match in STEP_OUTPUT_PATTERN.finditer(value):
                if match.group(1) == step_name:
                    return True
        return False

    def _validate_output_references(
        self,
        value: Any,
        current_step: str,
        prior_steps: set,
        all_steps: set,
        source_file: Path,
        template_params: frozenset | None = None,
        _current_key: str | None = None,
    ) -> list[str]:
        """Validate step output references in parameter values.

        Finds all {{ steps.<name>.outputs.<path> }} patterns and validates that:
        1. The referenced step exists in the manifest
        2. The referenced step executes before the current step
        3. Self-references are only flagged if the template accepts that parameter
           (otherwise auto-filtering will remove it)

        Args:
            value: Parameter value to check (recursively handles dict/list/str)
            current_step: Name of the step using these parameters
            prior_steps: Set of step names that execute before current_step
            all_steps: Set of all step names in the manifest
            source_file: Parameter file path for error messages
            template_params: Set of parameter names the template accepts.
                            If None, self-references are always flagged (conservative).
            _current_key: Internal - tracks the top-level parameter key during recursion

        Returns:
            List of validation error messages
        """
        errors: list[str] = []

        if isinstance(value, dict):
            for key, val in value.items():
                # Track top-level key for self-reference validation
                top_level_key = _current_key if _current_key is not None else key
                errors.extend(
                    self._validate_output_references(
                        val,
                        current_step,
                        prior_steps,
                        all_steps,
                        source_file,
                        template_params,
                        top_level_key,
                    )
                )
        elif isinstance(value, list):
            for item in value:
                errors.extend(
                    self._validate_output_references(
                        item,
                        current_step,
                        prior_steps,
                        all_steps,
                        source_file,
                        template_params,
                        _current_key,
                    )
                )
        elif isinstance(value, str):
            for match in STEP_OUTPUT_PATTERN.finditer(value):
                ref_step = match.group(1)

                if ref_step not in all_steps:
                    errors.append(f"Step '{current_step}' references unknown step '{ref_step}' in {source_file}")
                elif ref_step == current_step:
                    # Self-reference: only error if template actually accepts this parameter
                    if template_params is None:
                        # No template info available - be conservative and flag it
                        errors.append(f"Step '{current_step}' cannot reference its own outputs in {source_file}")
                    elif _current_key is not None and _current_key in template_params:
                        # Template accepts this parameter - genuine circular dependency
                        errors.append(
                            f"Step '{current_step}' cannot reference its own outputs "
                            f"for parameter '{_current_key}' in {source_file}"
                        )
                    # else: auto-filtering will remove this parameter, so no error
                elif ref_step not in prior_steps:
                    errors.append(
                        f"Step '{current_step}' references step '{ref_step}' which runs later in {source_file}"
                    )

        return errors

    def show_plan(
        self,
        manifest_path: Path,
        selector: str | None = None,
    ) -> None:
        """Display deployment plan without executing.

        Shows which sites will be deployed to and what steps will run.
        Called by 'validate -v' to show the plan after validation passes.

        Args:
            manifest_path: Path to manifest file
            selector: Optional site selector
        """
        manifest = Manifest.from_file(manifest_path)
        sites = self.resolve_sites(manifest, selector)

        if not sites:
            print(f"⚠ No sites matched for manifest '{manifest.name}'")
            if selector:
                print(f"  Selector: {selector}")
            elif manifest.site_selector:
                print(f"  Manifest siteSelector: {manifest.site_selector}")
            print()
            return

        print(f"{'═'*60}")
        print(f"  DEPLOYMENT PLAN: {manifest.name}")
        if selector:
            print(f"  (filtered by: {selector})")
        print(f"{'═'*60}")

        if manifest.description:
            print(f"\n  {manifest.description}")

        print(f"\n  Sites ({len(sites)}):")
        for site in sites:
            print(f"    • {site.name} ({site.location})")

        print(f"\n  Parallel: {manifest.parallel}")

        print(f"\n  Steps ({len(manifest.steps)}):")
        for i, step in enumerate(manifest.steps, 1):
            condition_info = f" [when: {step.when}]" if step.when else ""

            if isinstance(step, KubectlStep):
                print(f"    {i}. {step.name} (kubectl:{step.operation}){condition_info}")
                print(f"       ├─ cluster: {step.arc.name}")
                for j, f in enumerate(step.files):
                    prefix = "└─" if j == len(step.files) - 1 else "├─"
                    print(f"       {prefix} {f}")
            else:
                print(f"    {i}. {step.name} ({step.scope}){condition_info}")
                print(f"       └─ {step.template}")

        print(f"\n{'═'*60}")
        total = sum(1 for site in sites for step in manifest.steps if self._evaluate_condition(step.when, site))
        print(f"  Total: {total} operation(s)")

        if len(sites) > 1:
            if manifest.parallel.is_sequential:
                print("  Execution: Sequential (one site at a time)")
            elif manifest.parallel.is_unlimited:
                print("  Execution: Parallel (all sites concurrently)")
            else:
                print(f"  Execution: Parallel (max {manifest.parallel.sites} concurrent)")
        print(f"{'═'*60}\n")

    def deploy(
        self,
        manifest_path: Path,
        selector: str | None = None,
        parallel_override: int | None = None,
        manifest: Manifest | None = None,
        sites: list[Site] | None = None,
    ) -> dict[str, Any]:
        """Execute deployment from manifest.

        Args:
            manifest_path: Path to manifest file
            selector: Optional site selector
            parallel_override: Override manifest's parallel.sites setting.
                              None = use manifest setting.
            manifest: Pre-loaded manifest (avoids re-parsing)
            sites: Pre-resolved sites (avoids re-resolving)

        Returns:
            Dict with deployment results keyed by site name and summary
        """
        if manifest is None:
            manifest = Manifest.from_file(manifest_path)
        if sites is None:
            sites = self.resolve_sites(manifest, selector)

        if not sites:
            logger.warning("No sites to deploy to")
            return {
                "sites": {},
                "summary": {
                    "total": 0,
                    "succeeded": 0,
                    "failed": 0,
                    "elapsed": 0.0,
                },
            }

        # Determine effective parallelism
        if parallel_override is not None:
            effective_parallel = ParallelConfig(sites=parallel_override)
        else:
            effective_parallel = manifest.parallel

        logger.info(f"Deploying '{manifest.name}' to {len(sites)} site(s) " f"(parallel: {effective_parallel})")

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        start_time = time.time()

        # Group sites by subscription
        site_groups = self._group_sites_by_subscription(sites)

        # Check if we have subscription-scoped steps
        has_sub_steps = self._has_subscription_scoped_steps(manifest)

        results: list[dict[str, Any]] = []
        subscription_outputs: SubscriptionOutputs = {}

        if has_sub_steps:
            # Build map of subscription_id -> subscription-level site
            subscription_sites: dict[str, Site] = {}
            rg_sites: list[Site] = []
            for sub_id, (sub_level, rg_level) in site_groups.items():
                if sub_level:
                    # Use first subscription-level site (validation ensures only one)
                    subscription_sites[sub_id] = sub_level[0]
                rg_sites.extend(rg_level)

            # Phase 1: Execute subscription-scoped steps
            subscription_outputs, sub_results = self._collect_subscription_outputs(
                manifest, subscription_sites, timestamp, effective_parallel
            )
            results.extend(sub_results)

            # Identify failed subscriptions and filter blocked sites
            failed_subscriptions = {
                sub_id
                for sub_id, site in subscription_sites.items()
                if any(r["site"] == site.name and r["status"] == "failed" for r in sub_results)
            }

            # Filter RG-level sites: block those with dependencies on failed subscriptions
            if failed_subscriptions and rg_sites:
                sub_step_names = self._get_subscription_step_names(manifest)
                proceeding_sites = []
                for site in rg_sites:
                    if site.subscription in failed_subscriptions:
                        if self._site_depends_on_subscription_outputs(manifest, site, sub_step_names):
                            # Site depends on failed subscription outputs - block it
                            _thread_safe_print(
                                f"[{site.name}] - blocked "
                                "(subscription deployment failed, site depends on its outputs)"
                            )
                            results.append(
                                {
                                    "site": site.name,
                                    "status": "blocked",
                                    "error": "Subscription deployment failed and site depends on its outputs",
                                    "steps_completed": 0,
                                    "steps_skipped": len(manifest.steps),
                                    "steps_total": len(manifest.steps),
                                    "elapsed": 0.0,
                                    "steps": [],
                                }
                            )
                        else:
                            # Site doesn't depend on subscription outputs - let it proceed
                            proceeding_sites.append(site)
                    else:
                        # Site is in a different subscription - unaffected
                        proceeding_sites.append(site)
                rg_sites = proceeding_sites

            # Phase 2: Execute RG-scoped steps for all RG-level sites
            if rg_sites:
                print(f"\n  [Phase 2] Resource group-scoped steps: {len(rg_sites)} site(s)")
                if effective_parallel.is_sequential or len(rg_sites) == 1:
                    rg_results = self._deploy_sequential(manifest, rg_sites, timestamp, subscription_outputs)
                else:
                    rg_results = self._deploy_parallel(
                        manifest, rg_sites, timestamp, effective_parallel, subscription_outputs
                    )
                results.extend(rg_results)
        else:
            # No subscription-scoped steps - simple execution
            if effective_parallel.is_sequential or len(sites) == 1:
                results = self._deploy_sequential(manifest, sites, timestamp)
            else:
                results = self._deploy_parallel(manifest, sites, timestamp, effective_parallel)

        total_elapsed = time.time() - start_time

        # Build summary
        succeeded = sum(1 for r in results if r["status"] == "success")
        failed = sum(1 for r in results if r["status"] == "failed")

        summary = {
            "total": len(results),
            "succeeded": succeeded,
            "failed": failed,
            "elapsed": total_elapsed,
        }

        # Print summary
        self._print_deployment_summary(results, total_elapsed)

        return {
            "sites": {r["site"]: r for r in results},
            "summary": summary,
        }
