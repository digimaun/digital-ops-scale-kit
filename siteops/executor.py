"""Azure CLI and kubectl executor for deployments.

This module handles the low-level execution of:
- Azure deployment commands (az deployment group/sub create)
- kubectl commands via Arc-connected cluster proxy
- Template parameter extraction for filtering

The module automatically configures Azure CLI User-Agent tracking
(AZURE_HTTP_USER_AGENT) to include "siteops/{version}" for usage
telemetry in Azure Activity Logs.
"""

import json
import logging
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, FrozenSet, Generator, List, Optional, Tuple

from siteops import __version__

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# User-Agent Configuration
# ---------------------------------------------------------------------------
# Azure CLI reads AZURE_HTTP_USER_AGENT and appends it to outgoing requests.
# This allows tracking Site Ops usage in Azure telemetry and activity logs.
# Format follows Azure SDK conventions: "tool-name/version"
# ---------------------------------------------------------------------------


def _configure_user_agent() -> None:
    """Configure Azure CLI User-Agent to include Site Ops identifier.

    Sets the AZURE_HTTP_USER_AGENT environment variable, which Azure CLI
    appends to all ARM requests. This enables usage tracking in:
    - Azure Activity Logs
    - Azure Telemetry

    The User-Agent follows Azure SDK conventions: "siteops/{version}"

    If AZURE_HTTP_USER_AGENT is already set, the Site Ops identifier is
    appended (if not already present) to preserve existing values.
    """
    siteops_agent = f"siteops/{__version__}"
    existing_agent = os.environ.get("AZURE_HTTP_USER_AGENT", "")

    # Avoid duplicate entries if module is reloaded
    if siteops_agent in existing_agent:
        logger.debug("User-Agent already configured: %s", existing_agent)
        return

    if existing_agent:
        new_agent = f"{existing_agent} {siteops_agent}"
    else:
        new_agent = siteops_agent

    os.environ["AZURE_HTTP_USER_AGENT"] = new_agent
    logger.debug("Configured AZURE_HTTP_USER_AGENT: %s", new_agent)


# Configure User-Agent on module import
_configure_user_agent()

# ---------------------------------------------------------------------------
# Thread Safety Locks
# ---------------------------------------------------------------------------

# Lock for thread-safe tmp_dir initialization
_tmp_dir_lock = threading.Lock()

# Lock for allocating unique Arc proxy ports
_arc_port_lock = threading.Lock()

# Track allocated Arc proxy port slots to avoid conflicts
# Each slot represents a (api_server_port, internal_port) pair
_allocated_arc_port_slots: set[int] = set()

# URL pattern - only HTTPS allowed for security
HTTPS_URL_PATTERN = re.compile(r"^https://", re.IGNORECASE)

# Time to wait for Arc proxy to establish connection (seconds)
ARC_PROXY_STARTUP_WAIT = int(os.environ.get("SITEOPS_ARC_PROXY_WAIT", "60"))

# Default timeout for Azure CLI deployments (60 minutes)
# Azure deployments can take significant time for complex resources
DEFAULT_AZ_TIMEOUT_SECONDS = 3600

# Default timeout for kubectl operations (10 minutes)
DEFAULT_KUBECTL_TIMEOUT_SECONDS = 600

# Arc proxy port configuration
# Each proxy needs 2 ports: api_server_port (--port) and internal_port (api_server_port - 1)
# We allocate slots with spacing of 10 to avoid conflicts
# Start at 47021 (not 47011) so slot 0 also triggers the fallback logic in Azure CLI
# This ensures internal port is always (api_server_port - 1), not the hardcoded 47010
ARC_PROXY_PORT_BASE = 47021  # First slot uses 47021/47020, avoiding default 47010
ARC_PROXY_PORT_SPACING = 10  # Space between slots
ARC_PROXY_MAX_SLOTS = 10  # Maximum concurrent proxies


@lru_cache(maxsize=128)
def get_template_parameters(template_path: str) -> FrozenSet[str]:
    """Extract parameter names from a Bicep or ARM template.

    For Bicep files, uses 'az bicep build --stdout' to convert to ARM JSON.
    For ARM JSON files, parses directly.

    Results are cached per template path for performance.

    Args:
        template_path: Absolute path to the template file

    Returns:
        Frozenset of parameter names the template accepts

    Raises:
        ValueError: If template cannot be parsed
        FileNotFoundError: If template file doesn't exist
    """
    path = Path(template_path)

    if not path.exists():
        raise FileNotFoundError(f"Template not found: {template_path}")

    if path.suffix == ".bicep":
        az_path = shutil.which("az")
        if not az_path:
            raise ValueError("Azure CLI (az) not found in PATH. Required for Bicep template parsing.")

        result = subprocess.run(
            [az_path, "bicep", "build", "--file", str(path), "--stdout"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise ValueError(f"Failed to compile Bicep template {template_path}: {result.stderr}")
        try:
            arm_json = json.loads(result.stdout)
        except json.JSONDecodeError as e:
            raise ValueError(f"Failed to parse compiled Bicep template {template_path}: {e}") from e
    elif path.suffix == ".json":
        try:
            with open(path, "r", encoding="utf-8") as f:
                arm_json = json.load(f)
        except json.JSONDecodeError as e:
            raise ValueError(f"Failed to parse ARM template {template_path}: {e}") from e
    else:
        raise ValueError(f"Unsupported template format: {path.suffix}. Expected .bicep or .json")

    parameters = arm_json.get("parameters", {})
    param_names = frozenset(parameters.keys())

    logger.debug(f"Template {path.name} accepts parameters: {sorted(param_names)}")
    return param_names


def filter_parameters(
    parameters: Dict[str, Any],
    template_path: str,
    step_name: str,
) -> Dict[str, Any]:
    """Filter parameters to only those accepted by the template.

    Args:
        parameters: All parameters provided for the step
        template_path: Absolute path to the template file
        step_name: Name of the step (for logging)

    Returns:
        Filtered parameters dict containing only keys the template accepts
    """
    accepted_params = get_template_parameters(template_path)

    filtered = {}
    unused = []

    for key, value in parameters.items():
        if key in accepted_params:
            filtered[key] = value
        else:
            unused.append(key)

    if unused:
        logger.debug(f"Step '{step_name}': Filtered out parameters not in template: {unused}")

    return filtered


def _allocate_arc_port_slot() -> int:
    """Allocate a unique port slot for Arc proxy.

    Returns:
        The api_server_port to use (internal port will be this - 1)

    Raises:
        RuntimeError: If no slots are available.
    """
    with _arc_port_lock:
        for slot in range(ARC_PROXY_MAX_SLOTS):
            if slot not in _allocated_arc_port_slots:
                _allocated_arc_port_slots.add(slot)
                port = ARC_PROXY_PORT_BASE + (slot * ARC_PROXY_PORT_SPACING)
                logger.debug(f"Allocated Arc proxy slot {slot} (port {port})")
                return port
        raise RuntimeError(f"No available Arc proxy slots (max {ARC_PROXY_MAX_SLOTS} concurrent proxies)")


def _release_arc_port_slot(port: int) -> None:
    """Release an allocated Arc proxy port slot."""
    with _arc_port_lock:
        slot = (port - ARC_PROXY_PORT_BASE) // ARC_PROXY_PORT_SPACING
        _allocated_arc_port_slots.discard(slot)
        logger.debug(f"Released Arc proxy slot {slot} (port {port})")


@dataclass
class DeploymentResult:
    """Result of a Bicep/ARM deployment operation.

    Attributes:
        success: Whether the deployment succeeded
        step_name: Name of the step that was executed
        site_name: Name of the site deployed to
        deployment_name: Azure deployment name
        outputs: Deployment outputs (from Bicep/ARM)
        error: Error message if deployment failed
    """

    success: bool
    step_name: str
    site_name: str
    deployment_name: str
    outputs: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None


@dataclass
class KubectlResult:
    """Result of a kubectl operation.

    Attributes:
        success: Whether the operation succeeded
        step_name: Name of the step that was executed
        site_name: Name of the site
        error: Error message if operation failed
    """

    success: bool
    step_name: str
    site_name: str
    error: Optional[str] = None


class AzCliExecutor:
    """Executes Azure CLI deployments and kubectl operations.

    Handles:
    - Resource group and subscription-scoped ARM/Bicep deployments
    - kubectl apply via Arc-connected cluster proxy

    Attributes:
        workspace: Path to the Site Ops workspace directory
        dry_run: If True, commands are logged but not executed
    """

    def __init__(self, workspace: Path, dry_run: bool = False):
        self.workspace = workspace
        self.dry_run = dry_run
        self._tmp_dir: Optional[Path] = None
        self._az_path: Optional[str] = None
        self._kubectl_path: Optional[str] = None

    @property
    def az_path(self) -> Optional[str]:
        """Find and cache the az CLI executable path."""
        if self._az_path is None:
            self._az_path = shutil.which("az")
        return self._az_path

    @property
    def kubectl_path(self) -> Optional[str]:
        """Find and cache the kubectl executable path."""
        if self._kubectl_path is None:
            self._kubectl_path = shutil.which("kubectl")
        return self._kubectl_path

    @property
    def tmp_dir(self) -> Path:
        """Get or create the temp directory for parameter files.

        Uses double-checked locking for thread-safe initialization.
        """
        if self._tmp_dir is None:
            with _tmp_dir_lock:
                if self._tmp_dir is None:
                    self._tmp_dir = self.workspace / ".siteops" / "tmp"
                    self._tmp_dir.mkdir(parents=True, exist_ok=True)
        return self._tmp_dir

    def _run_az(self, args: List[str], timeout: int = DEFAULT_AZ_TIMEOUT_SECONDS) -> Tuple[bool, str, str]:
        """Run an Azure CLI command.

        Args:
            args: Command arguments (without 'az' prefix)
            timeout: Command timeout in seconds (default: 60 minutes)

        Returns:
            Tuple of (success, stdout, stderr)
        """
        if not self.az_path:
            return False, "", "Azure CLI (az) not found in PATH. Install from https://aka.ms/installazurecli"

        cmd = [self.az_path] + args
        cmd_str = " ".join(cmd)

        if self.dry_run:
            logger.info(f"[DRY-RUN] {cmd_str}")
            return True, "{}", ""

        logger.debug(f"Executing: {cmd_str}")

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=timeout)
            return result.returncode == 0, result.stdout, result.stderr
        except subprocess.TimeoutExpired:
            return False, "", f"Command timed out after {timeout}s"
        except Exception as e:
            return False, "", f"Failed to execute az command: {e}"

    def _run_kubectl(self, args: List[str], timeout: int = DEFAULT_KUBECTL_TIMEOUT_SECONDS) -> Tuple[bool, str, str]:
        """Run a kubectl command.

        Args:
            args: Command arguments (without 'kubectl' prefix)
            timeout: Command timeout in seconds (default: 10 minutes)

        Returns:
            Tuple of (success, stdout, stderr)
        """
        if not self.kubectl_path:
            return False, "", "kubectl not found in PATH. Install from https://kubernetes.io/docs/tasks/tools/"

        cmd = [self.kubectl_path] + args
        cmd_str = " ".join(cmd)

        if self.dry_run:
            logger.info(f"[DRY-RUN] {cmd_str}")
            return True, "", ""

        logger.debug(f"Executing: {cmd_str}")

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=timeout)
            return result.returncode == 0, result.stdout, result.stderr
        except subprocess.TimeoutExpired:
            return False, "", f"Command timed out after {timeout}s"
        except Exception as e:
            return False, "", f"Failed to execute kubectl command: {e}"

    @contextmanager
    def _arc_proxy(
        self,
        cluster_name: str,
        resource_group: str,
        subscription: str,
    ) -> Generator[bool, None, None]:
        """Context manager for Arc-connected cluster proxy.

        Starts `az connectedk8s proxy` in the background, waits for it to
        establish, and ensures cleanup on exit (even on exceptions).

        Uses unique port slots for each proxy to support parallel deployments.
        Each slot uses --port N where internal port becomes N-1.

        Args:
            cluster_name: Name of the Arc-connected cluster
            resource_group: Resource group containing the cluster
            subscription: Azure subscription ID

        Yields:
            True if proxy started successfully, False otherwise

        Example:
            with self._arc_proxy("my-cluster", "my-rg", "sub-id") as ready:
                if ready:
                    self._run_kubectl(["apply", "-f", "config.yaml"])
        """
        if self.dry_run:
            logger.info(
                f"[DRY-RUN] az connectedk8s proxy -n {cluster_name} "
                f"-g {resource_group} --subscription {subscription}"
            )
            yield True
            return

        if not self.az_path:
            logger.error("Azure CLI not found - cannot start Arc proxy")
            yield False
            return

        proxy_process: Optional[subprocess.Popen] = None
        allocated_port: Optional[int] = None

        try:
            # Allocate a unique port slot for this proxy instance
            allocated_port = _allocate_arc_port_slot()

            cmd = [
                self.az_path,
                "connectedk8s",
                "proxy",
                "-n",
                cluster_name,
                "-g",
                resource_group,
                "--subscription",
                subscription,
                "--port",
                str(allocated_port),
            ]

            logger.debug(f"Starting Arc proxy: {' '.join(cmd)}")

            # Start process with its own process group for clean termination
            if os.name == "nt":
                # Windows: use CREATE_NEW_PROCESS_GROUP for signal handling
                proxy_process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
                )
            else:
                # Unix: use setsid to create new process group
                proxy_process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    preexec_fn=os.setsid,
                )

            logger.debug(f"Waiting {ARC_PROXY_STARTUP_WAIT}s for proxy to establish...")
            time.sleep(ARC_PROXY_STARTUP_WAIT)

            # Check if proxy is still running
            if proxy_process.poll() is not None:
                _, stderr = proxy_process.communicate(timeout=5)
                logger.error(f"Arc proxy exited unexpectedly: {stderr}")
                yield False
                return

            logger.debug("Arc proxy established successfully")
            yield True

        except Exception as e:
            logger.error(f"Failed to start Arc proxy: {e}")
            yield False

        finally:
            if proxy_process is not None and proxy_process.poll() is None:
                logger.debug("Terminating Arc proxy...")
                try:
                    if os.name == "nt":
                        # Windows: send CTRL+BREAK to process group
                        proxy_process.send_signal(signal.CTRL_BREAK_EVENT)
                    else:
                        # Unix: send SIGTERM to process group
                        os.killpg(os.getpgid(proxy_process.pid), signal.SIGTERM)

                    proxy_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    logger.debug("Proxy did not terminate gracefully, forcing...")
                    proxy_process.kill()
                except Exception as e:
                    logger.debug(f"Error during proxy cleanup: {e}")
                    try:
                        proxy_process.kill()
                    except Exception:
                        pass

            # Release the allocated port slot
            if allocated_port is not None:
                _release_arc_port_slot(allocated_port)

    def _write_params_file(self, parameters: Dict[str, Any], step_name: str, site_name: str) -> Path:
        """Write parameters to a temp file in ARM parameter format.

        Args:
            parameters: Parameter key-value pairs
            step_name: Step name (for filename)
            site_name: Site name (for filename)

        Returns:
            Path to the created parameter file
        """
        arm_params = {
            "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
            "contentVersion": "1.0.0.0",
            "parameters": {k: {"value": v} for k, v in parameters.items()},
        }

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        filename = f"{site_name}-{step_name}-{timestamp}.json"

        tmp_dir = self.tmp_dir
        tmp_dir.mkdir(parents=True, exist_ok=True)

        params_path = tmp_dir / filename

        with open(params_path, "w", encoding="utf-8") as f:
            json.dump(arm_params, f, indent=2)

        return params_path

    def _deploy(
        self,
        args: List[str],
        parameters: Dict[str, Any],
        deployment_name: str,
        step_name: str,
        site_name: str,
    ) -> DeploymentResult:
        """Execute an Azure deployment and return results.

        Args:
            args: Base az deployment command arguments
            parameters: Parameters to pass to the deployment
            deployment_name: Name for the Azure deployment
            step_name: Site Ops step name
            site_name: Site Ops site name

        Returns:
            DeploymentResult with success status and outputs
        """
        if parameters:
            params_path = self._write_params_file(parameters, step_name, site_name)
            args.extend(["--parameters", f"@{params_path}"])

        success, stdout, stderr = self._run_az(args)

        outputs = {}
        if success and stdout and not self.dry_run:
            try:
                result = json.loads(stdout)
                outputs = result.get("properties", {}).get("outputs", {})
            except json.JSONDecodeError:
                pass

        return DeploymentResult(
            success=success,
            step_name=step_name,
            site_name=site_name,
            deployment_name=deployment_name,
            outputs=outputs,
            error=stderr if not success else None,
        )

    def deploy_resource_group(
        self,
        subscription: str,
        resource_group: str,
        template_path: Path,
        parameters: Dict[str, Any],
        deployment_name: str,
        step_name: str,
        site_name: str,
    ) -> DeploymentResult:
        """Deploy a Bicep/ARM template to a resource group.

        Args:
            subscription: Azure subscription ID
            resource_group: Target resource group name
            template_path: Path to the template file
            parameters: Deployment parameters
            deployment_name: Name for the Azure deployment
            step_name: Site Ops step name
            site_name: Site Ops site name

        Returns:
            DeploymentResult with success status and outputs
        """
        args = [
            "deployment",
            "group",
            "create",
            "--subscription",
            subscription,
            "--resource-group",
            resource_group,
            "--template-file",
            str(template_path),
            "--name",
            deployment_name,
            "--output",
            "json",
        ]
        return self._deploy(args, parameters, deployment_name, step_name, site_name)

    def deploy_subscription(
        self,
        subscription: str,
        location: str,
        template_path: Path,
        parameters: Dict[str, Any],
        deployment_name: str,
        step_name: str,
        site_name: str,
    ) -> DeploymentResult:
        """Deploy a Bicep/ARM template at subscription scope.

        Args:
            subscription: Azure subscription ID
            location: Azure region for deployment metadata
            template_path: Path to the template file
            parameters: Deployment parameters
            deployment_name: Name for the Azure deployment
            step_name: Site Ops step name
            site_name: Site Ops site name

        Returns:
            DeploymentResult with success status and outputs
        """
        args = [
            "deployment",
            "sub",
            "create",
            "--subscription",
            subscription,
            "--location",
            location,
            "--template-file",
            str(template_path),
            "--name",
            deployment_name,
            "--output",
            "json",
        ]
        return self._deploy(args, parameters, deployment_name, step_name, site_name)

    def _validate_kubectl_file(self, file_path: str) -> Tuple[bool, Optional[str]]:
        """Validate a kubectl file path or URL for security.

        Security checks:
        - URLs must be HTTPS (HTTP not allowed)
        - Local paths cannot traverse outside workspace

        Args:
            file_path: Local file path or URL

        Returns:
            Tuple of (is_valid, error_message)
        """
        if HTTPS_URL_PATTERN.match(file_path):
            return True, None

        if file_path.lower().startswith("http://"):
            return False, f"HTTP URLs not allowed for security (use HTTPS): {file_path}"

        try:
            resolved = (self.workspace / file_path).resolve()
            resolved.relative_to(self.workspace)
        except ValueError:
            return False, f"Path traversal not allowed (must be within workspace): {file_path}"

        if not resolved.exists():
            return False, f"File not found: {file_path}"

        return True, None

    def kubectl_apply(
        self,
        cluster_name: str,
        resource_group: str,
        subscription: str,
        files: List[str],
        step_name: str,
        site_name: str,
    ) -> KubectlResult:
        """Apply Kubernetes manifests to an Arc-connected cluster.

        Manages the full lifecycle:
        1. Start `az connectedk8s proxy` in background
        2. Wait for proxy to establish (~25 seconds)
        3. Run `kubectl apply -f` for all files
        4. Terminate proxy

        Args:
            cluster_name: Name of the Arc-connected cluster
            resource_group: Resource group containing the cluster
            subscription: Azure subscription ID
            files: List of file paths (workspace-relative) or HTTPS URLs
            step_name: Site Ops step name
            site_name: Site Ops site name

        Returns:
            KubectlResult with success status
        """
        # Validate all files first
        resolved_files: List[str] = []
        for file_path in files:
            is_valid, error = self._validate_kubectl_file(file_path)
            if not is_valid:
                return KubectlResult(
                    success=False,
                    step_name=step_name,
                    site_name=site_name,
                    error=error,
                )

            if HTTPS_URL_PATTERN.match(file_path):
                resolved_files.append(file_path)
            else:
                resolved_files.append(str((self.workspace / file_path).resolve()))

        if self.dry_run:
            files_display = ", ".join(files)
            logger.info(f"[DRY-RUN] kubectl apply via Arc proxy ({cluster_name}): {files_display}")
            return KubectlResult(success=True, step_name=step_name, site_name=site_name)

        if not self.kubectl_path:
            return KubectlResult(
                success=False,
                step_name=step_name,
                site_name=site_name,
                error="kubectl not found in PATH",
            )

        with self._arc_proxy(cluster_name, resource_group, subscription) as proxy_ready:
            if not proxy_ready:
                return KubectlResult(
                    success=False,
                    step_name=step_name,
                    site_name=site_name,
                    error="Failed to establish Arc proxy connection",
                )

            args = ["apply"]
            for f in resolved_files:
                args.extend(["-f", f])

            success, stdout, stderr = self._run_kubectl(args)

            if success and stdout:
                logger.debug(f"kubectl output:\n{stdout}")

            return KubectlResult(
                success=success,
                step_name=step_name,
                site_name=site_name,
                error=stderr if not success else None,
            )
