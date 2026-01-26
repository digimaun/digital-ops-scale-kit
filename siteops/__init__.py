"""
Site Ops - Multi-site Azure infrastructure orchestration.

Site Ops is a CLI tool for deploying Azure infrastructure across multiple
sites using Bicep/ARM templates with support for:

- Site definitions with labels, properties, and inheritance
- Manifest-based deployment orchestration
- Parameter templating with site variables and output chaining
- Parallel deployment with configurable concurrency
- kubectl operations via Arc-connected clusters

Usage:
    siteops -w <workspace> deploy <manifest>
    siteops -w <workspace> validate <manifest>
    siteops -w <workspace> sites

The package automatically configures Azure CLI User-Agent tracking
(AZURE_HTTP_USER_AGENT) to include "siteops/{version}" for usage
telemetry in Azure Activity Logs.
"""

__version__ = "0.1.0"
__all__ = ["__version__"]
