# Digital Operations Scale Kit

**Fleet-scale Azure infrastructure deployment, simplified.**

> [!NOTE]
> This project is under active development. If you're an Azure IoT Operations customer or interested in fleet-scale deployment, reach out at <azureiotoperationslicensinghelp@microsoft.com>.

Deploy Azure IoT Operations—or any Azure infrastructure—across dozens of sites with a single command. Per-site customization, parallel execution, and failure isolation built in.

```bash
# Deploy to all production sites
siteops -w workspaces/iot-operations deploy manifests/aio-install.yaml -l "environment=prod"
```

---

## What's in this repository

| Project | Description |
|---------|-------------|
| **Site Ops** | A reference implementation of a multi-site IaC orchestration tool. Template-agnostic—works with any Bicep or ARM templates. |
| **IoT Operations Workspace** | A starter kit demonstrating Site Ops for deploying Azure IoT Operations at scale. |

---

## Why Site Ops?

ARM/Bicep deploys resources. Site Ops orchestrates deployments across your fleet.

> **Site Ops isn't replacing ARM/Bicep—it's the fleet management layer on top.**

| Challenge | Site Ops Solution |
|-----------|-------------------|
| Deploying to 50+ sites manually | One command deploys to all matching sites in parallel |
| Targeting specific sites or environments | Label-based selection filters your fleet (`-l environment=prod`, `-l country=US`) |
| Per-site configuration differences | Template variables (`{{ site.name }}`, `{{ site.labels.X }}`) customize each deployment |
| Multi-step dependencies | Output chaining passes resource IDs between steps automatically |
| Partial failures stopping everything | Failure isolation—one site's failure doesn't block others |
| Environment-specific values mixed with code | Site overlays separate per-environment config from committed files |

### Portability

Site Ops runs anywhere Python runs—no agents, no servers, no state to manage.

- **Run anywhere** — Local machine, GitHub Actions, Azure DevOps, GitLab CI, or any CI/CD platform
- **Zero infrastructure** — No servers, agents, or state backends to provision
- **CI/CD agnostic** — Included GitHub Actions workflows serve as reference implementations; adapt to your preferred platform

### Key capabilities

- **One-command fleet deployment** — Deploy to all matching sites with a single command
- **Declarative site inventory** — Define your fleet as code—sites with labels, parameters, and inheritance
- **Label-based site selection** — Target any slice of your fleet: `-l environment=prod`, `-l country=US,city=Seattle`, or `-l name=munich-dev`
- **Subscription-scoped deployment** — Deploy shared resources once per subscription, then deploy per-site resources with automatic output resolution
- **Output chaining** — Reference outputs from previous steps, including cross-scope resolution from subscription to resource group deployments
- **Parallel execution** — Deploy to multiple sites simultaneously with configurable concurrency
- **Failure isolation** — One site's failure doesn't block others; subscription failures block only dependent sites
- **Dry-run validation** — Preview the full deployment plan without making Azure calls
- **Flexible step orchestration** — Conditional execution, parameter auto-filtering, and mixed step types (Bicep and kubectl via Arc proxy) in a single manifest

### Cloud-first deployment

Site Ops deploys infrastructure through Azure Resource Manager—the native control plane for Azure resources. For Arc-enabled solutions like Azure IoT Operations, this aligns with Azure's cloud-first model: no in-cluster GitOps agents required.

---

## Quick start

### Option 1: Use as a GitHub template (recommended)

1. **Create your repository**:
   - Click **Use this template** → **Create a new repository**
   - Or fork the repository to your organization

2. **Configure GitHub secrets** for Azure OIDC authentication:

   | Secret | Description |
   |--------|-------------|
   | `AZURE_CLIENT_ID` | Azure AD application client ID |
   | `AZURE_TENANT_ID` | Azure AD tenant ID |
   | `AZURE_SUBSCRIPTION_ID` | Default subscription for login |

   See [docs/ci-cd-setup.md](docs/ci-cd-setup.md) for OIDC federation setup.

3. **Configure site overrides** (optional):

   The included sites use placeholder subscription IDs. To deploy to real Azure resources, create a `SITE_OVERRIDES` secret with your actual values:

   ```json
   {
     "munich-dev": {
       "subscription": "your-subscription-id",
       "resourceGroup": "your-resource-group",
       "parameters.clusterName": "your-arc-cluster"
     }
   }
   ```

4. **Configure environments** (optional):
   - Create `dev`, `staging`, `prod` environments in repository settings
   - Add approval policies for `staging` and `prod`

5. **Run a deployment**:
   - Go to **Actions** → **Deploy** → **Run workflow**
   - Select a manifest and environment
   - Monitor progress in the workflow logs

### Option 2: Run locally

```bash
# Clone the repository
git clone https://github.com/Azure/digital-ops-scale-kit.git
cd digital-ops-scale-kit

# Install Site Ops
pip install -e .

# Authenticate with Azure
az login

# List available sites
siteops -w workspaces/iot-operations sites

# Validate manifest and all referenced files
siteops -w workspaces/iot-operations validate manifests/aio-install.yaml

# Dry run (show commands without executing)
siteops -w workspaces/iot-operations deploy manifests/aio-install.yaml --dry-run

# Deploy to dev sites
siteops -w workspaces/iot-operations deploy manifests/aio-install.yaml -l "environment=dev"
```

### Prerequisites

- Python 3.10+
- [Azure CLI](https://docs.microsoft.com/cli/azure/install-azure-cli) installed and authenticated
- For kubectl steps: `kubectl` in PATH

---

## Repository structure

```
digital-ops-scale-kit/
├── siteops/                      # Site Ops package
│   ├── cli.py                    # CLI entry point
│   ├── models.py                 # Site, Manifest, Step dataclasses
│   ├── orchestrator.py           # Core orchestration logic
│   └── executor.py               # Azure CLI and kubectl execution
├── tests/                        # Test suite
├── scripts/                      # Utility scripts (Bicep validation, etc.)
├── workspaces/
│   └── iot-operations/           # Reference implementation
│       ├── sites/                # Site definitions
│       ├── manifests/            # Deployment orchestration
│       ├── parameters/           # Parameter files
│       └── templates/            # Bicep templates
├── docs/                         # Extended documentation
│   ├── aio-releases.md           # AIO release pinning, upgrades, adding a new release
│   ├── ci-cd-setup.md            # GitHub Actions, Azure DevOps, OIDC, secrets
│   ├── e2e-testing.md            # End-to-end live-subscription test workflow
│   ├── manifest-reference.md     # Manifest syntax, step types
│   ├── parameter-resolution.md   # Variables, output chaining
│   ├── secret-sync.md            # Secret sync enablement and usage
│   ├── site-configuration.md     # Sites, inheritance, overlays
│   └── troubleshooting.md        # Common issues and solutions
├── .github/                      # GitHub Actions workflows
└── .pipelines/                   # Azure DevOps pipeline definitions
```

### Workspace anatomy

Each workspace follows a consistent structure:

| Directory | Purpose | Contains |
|-----------|---------|----------|
| `sites/` | **Where** to deploy | Site definitions with subscription, resource group, labels |
| `manifests/` | **What** to deploy | Ordered steps with site selection and conditions |
| `parameters/` | **With what values** | Template variables, output chaining |
| `templates/` | **How** to deploy | Bicep/ARM templates |
| `sites.local/` | **Overrides** | Local/CI overrides (gitignored) |

---

## Core concepts

### Sites

Sites define deployment targets. Sites operate at two levels:

| Site has | Site level | Deploys |
|----------|-----------|--------|
| `subscription` + `resourceGroup` | RG-level | Both subscription and RG-scoped steps |
| `subscription` only | Subscription-level | `scope: subscription` steps only |

**RG-level site** (most common):

```yaml
apiVersion: siteops/v1
kind: Site
name: munich-dev

subscription: "00000000-0000-0000-0000-000000000000"
resourceGroup: rg-iot-munich-dev
location: germanywestcentral

labels:
  environment: dev
  country: DE
  city: Munich

parameters:
  clusterName: munich-dev-arc
  brokerConfig:
    memoryProfile: Low
```

**Subscription-level site** (for shared resources):

```yaml
apiVersion: siteops/v1
kind: Site
name: germany-subscription

subscription: "00000000-0000-0000-0000-000000000000"
location: germanywestcentral
# No resourceGroup → subscription-level site

parameters:
  edgeSiteName: germany-edge-site
```

See [docs/site-configuration.md](docs/site-configuration.md) for inheritance, overlays, and SiteTemplate patterns.

### Manifests

Manifests define deployment steps and target sites:

```yaml
apiVersion: siteops/v1
kind: Manifest
name: aio-install
description: Deploy Azure IoT Operations
parallel: 3  # Deploy up to 3 sites concurrently

selector: "environment=dev"

parameters:
  - parameters/common/common.yaml  # Applied to all steps

steps:
  - name: global-edge-site
    template: templates/edge-site/subscription.bicep
    scope: subscription  # Deploys once per subscription
    when: "{{ site.properties.deployOptions.enableGlobalSite }}"

  - name: edge-site
    template: templates/edge-site/main.bicep
    scope: resourceGroup  # Deploys per-site
    when: "{{ site.properties.deployOptions.enableEdgeSite }}"

  - name: schema-registry
    template: templates/deps/schema-registry.bicep
    scope: resourceGroup

  # ... additional steps (adr-ns, aio-enablement) omitted for brevity

  - name: aio-instance
    template: templates/aio/instance.bicep
    scope: resourceGroup
    parameters:
      - parameters/inputs/aio-instance.yaml  # Uses outputs from previous steps

  # ... additional steps (schema-registry-role, secretsync) omitted for brevity
```

### Template variables

Reference site values in parameter files:

```yaml
# parameters/common/common.yaml (manifest-level)
location: "{{ site.location }}"
customLocationName: "{{ site.name }}-cl"
aioInstanceName: "{{ site.name }}-aio"
schemaRegistryName: "{{ site.name }}-sr"
adrNamespaceName: "{{ site.name }}-ns"
tags:
  environment: "{{ site.labels.environment }}"
  site: "{{ site.name }}"
```

```yaml
# parameters/inputs/aio-instance.yaml (step-level, for output chaining)
schemaRegistryId: "{{ steps.schema-registry.outputs.schemaRegistry.id }}"
adrNamespaceId: "{{ steps.adr-ns.outputs.adrNamespace.id }}"

# Cross-scope output chaining (subscription → resource group)
edgeSiteId: "{{ steps.global-edge-site.outputs.site.id }}"
```

See [docs/parameter-resolution.md](docs/parameter-resolution.md) for auto-filtering, merge order, and cross-scope resolution.

---

## Commands

| Command | Description |
|---------|-------------|
| `siteops sites` | List available sites |
| `siteops validate <manifest>` | Validate manifest and all references |
| `siteops deploy <manifest>` | Execute deployment |
| `siteops deploy <manifest> --dry-run` | Show commands without executing |

### Common options

| Option | Description | Commands | Examples |
|--------|-------------|----------|----------|
| `-w, --workspace` | Workspace directory (required) | All | `-w workspaces/iot-operations` |
| `-l, --selector` | Filter sites by label | All | `-l environment=prod`, `-l country=US,city=Seattle` |
| `-p, --parallel` | Override parallel site count | `deploy` | `-p 5`, `-p 0` (unlimited) |
| `-v, --verbose` | Verbose output | `validate`, `sites` | |

---

## Extending

### Create a new workspace

1. Create directory structure:

   ```
   workspaces/my-workspace/
   ├── sites/
   ├── manifests/
   ├── parameters/
   └── templates/
   ```

2. Add site definitions in `sites/`
3. Add or reference Bicep templates
4. Create manifests that orchestrate the deployment

### Add a new site

```yaml
# sites/seattle-prod.yaml
apiVersion: siteops/v1
kind: Site
name: seattle-prod
inherits: base-site.yaml  # Optional: inherit shared config

subscription: "00000000-0000-0000-0000-000000000000"
resourceGroup: rg-iot-seattle-prod
location: westus2

labels:
  environment: prod
  country: US
  city: Seattle

parameters:
  clusterName: seattle-prod-arc
```

### Add conditional steps

```yaml
steps:
  - name: optional-feature
    template: templates/feature.bicep
    scope: resourceGroup
    when: "{{ site.properties.featureOptions.enableFeature }}"
```

---

## CI/CD

This repository includes GitHub Actions workflows for automated deployment:

| Workflow | Description |
|----------|-------------|
| `deploy.yaml` | Manual deployment via GitHub UI |
| `ci.yaml` | CI validation (tests + manifest check) |
| `_siteops-deploy.yaml` | Reusable deployment workflow |

### Required secrets

| Secret | Required | Description |
|--------|----------|-------------|
| `AZURE_CLIENT_ID` | Yes | Azure AD application client ID |
| `AZURE_TENANT_ID` | Yes | Azure AD tenant ID |
| `AZURE_SUBSCRIPTION_ID` | Yes | Default subscription for OIDC login |
| `SITE_OVERRIDES` | No | JSON object with per-site subscription/resourceGroup overrides |

See [docs/ci-cd-setup.md](docs/ci-cd-setup.md) for detailed configuration.

---

## Documentation

| Document | Description |
|----------|-------------|
| [docs/site-configuration.md](docs/site-configuration.md) | Site definitions, inheritance, overlays |
| [docs/manifest-reference.md](docs/manifest-reference.md) | Manifest syntax, step types, conditions |
| [docs/parameter-resolution.md](docs/parameter-resolution.md) | Template variables, output chaining, auto-filtering |
| [docs/aio-releases.md](docs/aio-releases.md) | Pinning an AIO release per site, in-place upgrades, adding a new release |
| [docs/secret-sync.md](docs/secret-sync.md) | Secret sync enablement and usage |
| [docs/ci-cd-setup.md](docs/ci-cd-setup.md) | GitHub Actions, Azure DevOps, OIDC, secrets configuration |
| [docs/e2e-testing.md](docs/e2e-testing.md) | End-to-end live-subscription test workflow |
| [docs/troubleshooting.md](docs/troubleshooting.md) | Common issues and solutions |

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup and contribution guidelines.

---

## License

[MIT](LICENSE)
