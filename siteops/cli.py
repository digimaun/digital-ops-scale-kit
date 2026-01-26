"""Command-line interface for Azure Site Ops.

Commands:
    deploy   - Deploy a manifest to target sites
    validate - Validate manifest (use -v to show deployment plan)
    sites    - List available sites
"""

import argparse
import logging
import sys
from pathlib import Path
from typing import Any

from siteops import __version__
from siteops.orchestrator import Orchestrator


def setup_logging(verbose: bool = False):
    """Configure logging based on verbosity level."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(message)s",
    )
    if not verbose:
        logging.getLogger("siteops.executor").setLevel(logging.WARNING)


def resolve_manifest_path(manifest: Path, workspace: Path) -> Path:
    """Resolve manifest path - if relative, make it relative to workspace."""
    if manifest.is_absolute():
        return manifest
    return workspace / manifest


def cmd_deploy(args, orchestrator: Orchestrator) -> int:
    """Execute deployment."""
    manifest_path = resolve_manifest_path(args.manifest, args.workspace)

    if not manifest_path.exists():
        print(f"Error: Manifest not found: {manifest_path}", file=sys.stderr)
        return 1

    # Validate parallel override if provided
    parallel_override = getattr(args, "parallel", None)
    if parallel_override is not None and parallel_override < 0:
        print("Error: --parallel must be >= 0", file=sys.stderr)
        return 1

    from siteops.models import Manifest

    manifest = Manifest.from_file(manifest_path)
    sites = orchestrator.resolve_sites(manifest, getattr(args, "selector", None))

    if not sites:
        print("\n⚠ No sites matched. Nothing to deploy.\n")
        return 0

    if not manifest.steps:
        print("\n⚠ Manifest has no steps. Nothing to deploy.\n")
        return 0

    # Execute deployment
    result = orchestrator.deploy(
        manifest_path,
        selector=getattr(args, "selector", None),
        parallel_override=parallel_override,
        manifest=manifest,
        sites=sites,
    )

    # Return exit code based on results
    if result["summary"]["failed"] > 0:
        return 1
    return 0


def cmd_validate(args, orchestrator: Orchestrator) -> int:
    """Validate manifest and optionally show deployment plan."""
    manifest_path = resolve_manifest_path(args.manifest, args.workspace)

    if not manifest_path.exists():
        print(f"Error: Manifest not found: {manifest_path}", file=sys.stderr)
        return 1

    selector = getattr(args, "selector", None)
    verbose = getattr(args, "verbose", False)
    errors = orchestrator.validate(manifest_path, selector=selector)

    if errors:
        print(f"\n✗ Validation failed with {len(errors)} error(s):\n")
        for err in errors:
            print(f"  • {err}")
        print()
        return 1

    print(f"\n✓ Manifest is valid: {manifest_path.name}\n")

    # Show deployment plan if verbose
    if verbose:
        orchestrator.show_plan(manifest_path, selector=selector)

    return 0


def _print_value(value: Any, indent: int = 6) -> None:
    """Recursively print a value with proper indentation.

    Args:
        value: The value to print (can be dict, list, or scalar)
        indent: Number of spaces for indentation
    """
    prefix = " " * indent
    if isinstance(value, dict):
        for k, v in value.items():
            if isinstance(v, dict):
                print(f"{prefix}{k}:")
                _print_value(v, indent + 2)
            elif isinstance(v, list):
                if len(v) == 0:
                    print(f"{prefix}{k}: []")
                elif all(isinstance(item, (str, int, float, bool, type(None))) for item in v):
                    # Simple list - print inline
                    print(f"{prefix}{k}: {v}")
                else:
                    # Complex list - print each item
                    print(f"{prefix}{k}:")
                    for i, item in enumerate(v):
                        if isinstance(item, dict):
                            print(f"{prefix}  [{i}]:")
                            _print_value(item, indent + 4)
                        else:
                            print(f"{prefix}  - {item}")
            else:
                print(f"{prefix}{k}: {v}")
    elif isinstance(value, list):
        for i, item in enumerate(value):
            if isinstance(item, dict):
                print(f"{prefix}[{i}]:")
                _print_value(item, indent + 2)
            else:
                print(f"{prefix}- {item}")
    else:
        print(f"{prefix}{value}")


def cmd_sites(args, orchestrator: Orchestrator) -> int:
    """List available sites in the workspace."""
    all_sites = orchestrator.load_all_sites()

    # Filter by selector if provided
    selector_str = getattr(args, "selector", None)
    if selector_str:
        from siteops.models import parse_selector

        selector = parse_selector(selector_str)
        sites = [s for s in all_sites if s.matches_selector(selector)]
    else:
        sites = all_sites

    if not sites:
        if selector_str:
            print(f"\nNo sites matched selector: {selector_str}\n")
        else:
            print("\nNo sites found in workspace\n")
        return 0

    verbose = getattr(args, "verbose", False)

    # Display header
    print()
    print("═" * 60)
    print(f"  Available Sites ({len(sites)})")
    if selector_str:
        print(f"  (filtered by: {selector_str})")
    print("═" * 60)
    print()

    for site in sorted(sites, key=lambda s: s.name):
        print(f"  {site.name}")
        print(f"    subscription:   {site.subscription}")
        print(f"    resourceGroup:  {site.resource_group}")
        print(f"    location:       {site.location}")

        if site.labels:
            print("    labels:")
            for key, value in sorted(site.labels.items()):
                print(f"      {key}: {value}")

        if site.properties:
            print("    properties:")
            _print_value(site.properties, indent=6)

        if site.parameters:
            print("    parameters:")
            _print_value(site.parameters, indent=6)

        print()

    return 0


def main():
    """Main entry point for the Site Ops CLI."""
    parser = argparse.ArgumentParser(
        prog="siteops",
        description="Azure Site Ops - Multi-site Azure IaC orchestration",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  siteops -w workspace deploy manifests/iot-ops.yaml
  siteops -w workspace deploy manifests/iot-ops.yaml --dry-run
  siteops -w workspace deploy manifests/iot-ops.yaml -l environment=prod -p 5
  siteops -w workspace validate manifests/iot-ops.yaml -v
  siteops -w workspace sites -l region=eastus
""",
    )
    parser.add_argument("--version", action="version", version=f"siteops {__version__}")
    parser.add_argument(
        "-w",
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Workspace directory (default: current directory)",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # deploy command
    p_deploy = subparsers.add_parser(
        "deploy",
        help="Deploy manifest to target sites",
        description="Execute deployment of a manifest to one or more sites.",
    )
    p_deploy.add_argument("manifest", type=Path, help="Path to manifest file")
    p_deploy.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be deployed without executing",
    )
    p_deploy.add_argument(
        "-l",
        "--selector",
        type=str,
        help="Filter sites by labels (e.g., 'environment=prod')",
    )
    p_deploy.add_argument(
        "-p",
        "--parallel",
        type=int,
        default=None,
        metavar="N",
        help="Max concurrent sites (0=unlimited, 1=sequential). Overrides manifest.",
    )

    # validate command
    p_validate = subparsers.add_parser(
        "validate",
        help="Validate manifest and show plan",
        description="Validate manifest syntax, files, and references. Use -v to show deployment plan.",
    )
    p_validate.add_argument("manifest", type=Path, help="Path to manifest file")
    p_validate.add_argument(
        "-l",
        "--selector",
        type=str,
        help="Filter sites by labels (e.g., 'environment=prod')",
    )
    p_validate.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Show deployment plan after validation",
    )

    # sites command
    p_sites = subparsers.add_parser(
        "sites",
        help="List available sites",
        description="List all sites in the workspace, optionally filtered by labels.",
    )
    p_sites.add_argument(
        "-l",
        "--selector",
        type=str,
        help="Filter sites by labels (e.g., 'environment=prod')",
    )
    p_sites.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Show additional details (properties)",
    )

    args = parser.parse_args()

    # Setup logging - use verbose from subcommand if available, otherwise False
    verbose = getattr(args, "verbose", False)
    setup_logging(verbose)

    args.workspace = Path(args.workspace).resolve()

    orchestrator = Orchestrator(
        workspace=args.workspace,
        dry_run=getattr(args, "dry_run", False),
    )

    commands = {
        "deploy": cmd_deploy,
        "validate": cmd_validate,
        "sites": cmd_sites,
    }

    exit_code = commands[args.command](args, orchestrator)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
