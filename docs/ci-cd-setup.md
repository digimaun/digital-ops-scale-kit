# CI/CD Setup

This guide covers GitHub Actions configuration for automated deployments.

## Prerequisites

1. Azure subscription with resources to deploy
2. GitHub repository with Actions enabled
3. Azure AD application for OIDC authentication

## Workflows

| Workflow | Trigger | Purpose |
|----------|---------|---------|
| `ci.yaml` | Push, pull request, manual | Run unit tests and validate manifests |
| `deploy.yaml` | Manual (`workflow_dispatch`) | Deploy infrastructure to Azure |
| `_siteops-deploy.yaml` | Called by deploy.yaml | Reusable deployment logic |

## Azure OIDC Configuration

OIDC (OpenID Connect) allows GitHub Actions to authenticate to Azure without storing secrets. Examples use bash syntax.

### 1. Create Azure AD application

```bash
# Create app registration
az ad app create --display-name "siteops-github-actions"

# Note the appId (client ID) from output
APP_ID=$(az ad app list --display-name "siteops-github-actions" --query "[0].appId" -o tsv)

# Create service principal
az ad sp create --id $APP_ID
```

### 2. Create federated credentials

```bash
# For main branch deployments
az ad app federated-credential create \
  --id $APP_ID \
  --parameters '{
    "name": "github-main",
    "issuer": "https://token.actions.githubusercontent.com",
    "subject": "repo:YOUR-ORG/YOUR-REPO:ref:refs/heads/main",
    "audiences": ["api://AzureADTokenExchange"]
  }'

# For environment-based deployments (recommended)
for ENV in dev staging prod; do
  az ad app federated-credential create \
    --id $APP_ID \
    --parameters "{
      \"name\": \"github-env-$ENV\",
      \"issuer\": \"https://token.actions.githubusercontent.com\",
      \"subject\": \"repo:YOUR-ORG/YOUR-REPO:environment:$ENV\",
      \"audiences\": [\"api://AzureADTokenExchange\"]
    }"
done
```

Alternatively, configure the subject to match a branch (`ref:refs/heads/main`), pull request (`pull_request`), or tag (`ref:refs/tags/v*`) instead of an environment.

### 3. Assign Azure roles

For basic deployments, Contributor is sufficient:

```bash
az role assignment create \
  --assignee $APP_ID \
  --role "Contributor" \
  --scope /subscriptions/<subscription-id>
```

**For AIO deployments:** The full installation includes RBAC operations (e.g., granting the AIO extension access to the schema registry). Contributor cannot create role assignments. Use Owner with a condition that prevents privilege escalation:

```bash
az role assignment create \
  --assignee $APP_ID \
  --role "Owner" \
  --scope /subscriptions/<subscription-id> \
  --condition $'((!(ActionMatches{\'Microsoft.Authorization/roleAssignments/write\'})) OR (@Request[Microsoft.Authorization/roleAssignments:RoleDefinitionId] ForAnyOfAllValues:GuidNotEquals {8e3af657-a8ff-443c-a75c-2fe8c4bcb635, 18d7d88d-d35e-4fb5-a5c3-7773c20a72d9, f58310d9-a9f6-439a-9e8d-f62e7b41a168})) AND ((!(ActionMatches{\'Microsoft.Authorization/roleAssignments/delete\'})) OR (@Resource[Microsoft.Authorization/roleAssignments:RoleDefinitionId] ForAnyOfAllValues:GuidNotEquals {8e3af657-a8ff-443c-a75c-2fe8c4bcb635, 18d7d88d-d35e-4fb5-a5c3-7773c20a72d9, f58310d9-a9f6-439a-9e8d-f62e7b41a168}))' \
  --condition-version "2.0"
```

This condition allows creating and deleting role assignments but blocks these privileged roles:

| GUID | Role |
| ---- | ---- |
| `8e3af657-a8ff-443c-a75c-2fe8c4bcb635` | Owner |
| `18d7d88d-d35e-4fb5-a5c3-7773c20a72d9` | User Access Administrator |
| `f58310d9-a9f6-439a-9e8d-f62e7b41a168` | Role Based Access Control Administrator |

### 4. Configure GitHub secrets

Go to **Settings → Secrets and variables → Actions** and add:

| Secret | Required | Description |
|--------|----------|-------------|
| `AZURE_CLIENT_ID` | Yes | Azure AD application client ID |
| `AZURE_TENANT_ID` | Yes | Azure AD tenant ID |
| `AZURE_SUBSCRIPTION_ID` | Yes | Default subscription for OIDC login |
| `SITE_OVERRIDES` | No | JSON object with per-site overrides (see below) |

### 5. Configure GitHub environments

Go to **Settings → Environments** and create:

#### `dev` environment

- No protection rules (deploys immediately)

#### `staging` environment

- Required reviewers: 1 person
- Deployment branches: `main` only

#### `prod` environment

- Required reviewers: 2 people
- Deployment branches: `main` only
- Wait timer: 5 minutes (optional)

## SITE_OVERRIDES (Optional)

Use `SITE_OVERRIDES` when you prefer not to commit configuration values (subscriptions, resource groups, credentials) to the repository. The workflow generates `sites.local/*.yaml` files at runtime from this secret.

**When to use:**

- You want to keep committed site files as templates with placeholder values
- Different CI environments target different resources
- Your team prefers separation between code and environment configuration

**When not needed:**

- Site files already contain real values
- You're comfortable committing configuration to the repository

### Format

Override subscription, resource group, and parameters per site. Supports nested paths using dot notation (e.g., `parameters.clusterName`):

```json
{
  "munich-dev": {
    "subscription": "00000000-0000-0000-0000-000000000000",
    "resourceGroup": "rg-aio-munich-dev",
    "parameters.clusterName": "arc-muc-dev-01"
  },
  "munich-prod": {
    "subscription": "00000000-0000-0000-0000-000000000000",
    "resourceGroup": "rg-aio-munich-prod",
    "parameters.clusterName": "arc-muc-prod-01"
  },
  "seattle-dev": {
    "subscription": "00000000-0000-0000-0000-000000000000",
    "resourceGroup": "rg-aio-seattle-dev",
    "parameters.clusterName": "arc-sea-dev-01"
  },
  "seattle-prod": {
    "subscription": "00000000-0000-0000-0000-000000000000",
    "resourceGroup": "rg-aio-seattle-prod",
    "parameters.clusterName": "arc-sea-prod-01"
  },
  "chicago-staging": {
    "subscription": "00000000-0000-0000-0000-000000000000",
    "resourceGroup": "rg-aio-chicago-staging",
    "parameters.clusterName": "arc-chi-staging-01"
  }
}
```

> **Note:** `SITE_OVERRIDES` is stored as a secret for access control (admin-only modification).
> Individual override values are registered with `::add-mask::` to prevent exposure in workflow logs.

## Running Deployments

### CI (automatic)

CI runs automatically on pushes to main and PRs that modify:

- `siteops/**`
- `workspaces/**`
- `tests/**`
- `pyproject.toml`

Can also be triggered manually from **Actions → CI → Run workflow**.

### Deploy via GitHub UI

1. Go to **Actions** tab
2. Select **"Deploy Infrastructure"**
3. Click **"Run workflow"**
4. Fill in options:
   - **Git ref**: Branch, tag, or commit (optional)
   - **Workspace**: Workspace name (default: `iot-operations`)
   - **Manifest**: Manifest to deploy (default: `aio-install`)
   - **Environment**: `dev`, `staging`, or `prod`
   - **Selector**: Additional site filter (optional, e.g., `region=eastus`)
   - **Dry run**: Preview only, no actual deployment
5. Click **"Run workflow"**

### Deploy via GitHub CLI

```bash
gh workflow run deploy.yaml \
  -f workspace=iot-operations \
  -f manifest=aio-install \
  -f environment=dev
```

Add `-f selector="<value>"` to filter sites further:

- `selector="country=US"` — sites with country label
- `selector="name=seattle-dev"` — specific site by name
- `selector="country=US,name=seattle-dev"` — multiple filters

### Deploy via REST API

```bash
curl -X POST \
  -H "Authorization: token $GITHUB_TOKEN" \
  -H "Accept: application/vnd.github.v3+json" \
  https://api.github.com/repos/YOUR-ORG/YOUR-REPO/actions/workflows/deploy.yaml/dispatches \
  -d '{
    "ref": "main",
    "inputs": {
      "manifest": "aio-install",
      "environment": "dev",
      "selector": "",
      "dry-run": "false"
    }
  }'
```

## Demo Scenarios

The iot-operations workspace demonstrates key Site Ops capabilities:

| Step | Manifest | Environment | Sites | Demonstrates |
|------|----------|-------------|-------|--------------|
| 1 | `aio-install` | `staging` | chicago-staging | Base AIO platform only |
| 2 | `aio-install` | `dev` | munich-dev, seattle-dev | Parallel deployment + simulator |
| 3 | `aio-install` | `prod` | munich-prod, seattle-prod | Parallel deployment, no simulator |
| 4 | `opc-ua-solution` | `staging` | chicago-staging | Solution layer on existing AIO |

### Site configuration

| Site | Environment | `includeSolution` | `includeOpcPlcSimulator` |
|------|-------------|-------------------|-------------------------|
| munich-dev | dev | ✅ | ✅ |
| seattle-dev | dev | ✅ | ✅ |
| munich-prod | prod | ✅ | ❌ |
| seattle-prod | prod | ✅ | ❌ |
| chicago-staging | staging | ❌ | ❌ |

### Running the demo

```bash
# Step 1: Deploy base AIO to staging (no solution)
gh workflow run deploy.yaml -f workspace="iot-operations" -f manifest="aio-install" -f environment="staging"

# Step 2: Deploy full stack to dev (parallel, with simulator)
gh workflow run deploy.yaml -f workspace="iot-operations" -f manifest="aio-install" -f environment="dev"

# Step 3: Deploy full stack to prod (parallel, no simulator)
gh workflow run deploy.yaml -f workspace="iot-operations" -f manifest="aio-install" -f environment="prod"

# Step 4: Add solution layer to staging
gh workflow run deploy.yaml -f workspace="iot-operations" -f manifest="opc-ua-solution" -f environment="staging"
```

## Workflow Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    Trigger Sources                          │
├─────────────┬─────────────┬─────────────┬──────────────────┤
│  GitHub UI  │  REST API   │  GitHub CLI │  Pull Request    │
└──────┬──────┴──────┬──────┴──────┬──────┴────────┬─────────┘
       │             │             │               │
       ▼             ▼             ▼               ▼
┌─────────────────────────┐   ┌─────────────────────────────┐
│     deploy.yaml         │   │          ci.yaml            │
│  (workflow_dispatch)    │   │  (push + pull_request)      │
└───────────┬─────────────┘   ├─────────────────────────────┤
            │                 │  • Unit Tests               │
            │                 │  • Manifest Validation      │
            │                 │  • Deployment Plan Preview  │
            ▼                 └─────────────────────────────┘
┌─────────────────────────────────────────────────────────────┐
│               _siteops-deploy.yaml (reusable)               │
├─────────────────────────────────────────────────────────────┤
│  1. Setup Site Ops                                          │
│  2. Validate inputs (path traversal protection)             │
│  3. Generate sites.local/ from SITE_OVERRIDES secret        │
│  4. Validate and show deployment plan                       │
│  5. Azure Login (OIDC)                                      │
│  6. Start OIDC token refresh service (background)           │
│  7. Run: siteops deploy                                     │
│  8. Azure Logout                                            │
└─────────────────────────────────────────────────────────────┘
```

## Security

| Feature | Description |
|---------|-------------|
| **OIDC Authentication** | No stored Azure credentials; tokens are short-lived |
| **Environment Protection** | Required approvals for staging/prod |
| **Input Validation** | Prevents path traversal and injection attacks |
| **Site Name Sanitization** | SITE_OVERRIDES keys validated against `^[a-zA-Z0-9_-]+$` |
| **Override Value Masking** | Individual SITE_OVERRIDES values registered with `::add-mask::` to prevent log exposure |
| **Concurrency Control** | One deployment per environment at a time |
| **Least Privilege Permissions** | Workflows request minimal GitHub token scopes |
| **Token Refresh** | Background service refreshes OIDC token for long deployments |
| **Audit Trail** | All runs logged with triggering user |

### Security model

```
┌─────────────────────────────────────────────────────────────┐
│  Layer 1: GitHub                                            │
│  • Environment protection rules (approvals, branch gates)   │
│  • Concurrency prevents parallel deploys to same env        │
│  • Input validation blocks path traversal                   │
│  • Minimal permissions (contents: read, id-token: write)    │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│  Layer 2: OIDC Federation                                   │
│  • No stored Azure credentials                              │
│  • Token scoped to specific environment                     │
│  • Federated credential must match subject claim            │
│  • Automatic token refresh for long-running deployments     │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│  Layer 3: Azure RBAC                                        │
│  • Service principal has scoped permissions                 │
│  • Can further restrict by subscription/resource group      │
└─────────────────────────────────────────────────────────────┘
```

## Extending

### Adding new manifests

To add a new manifest to the deployment workflow:

1. Create your manifest in `workspaces/<workspace>/manifests/`
2. Update `.github/workflows/deploy.yaml` to add it to the `manifest` dropdown:

```yaml
manifest:
    description: "Manifest to deploy"
    required: true
    type: choice
    options:
        - aio-install
        - opc-ua-solution
        - my-new-manifest  # Add here (without .yaml extension)
```

### Adding new workspaces

To add a new workspace (e.g., `iot-hub`):

1. Create `workspaces/iot-hub/` with `manifests/`, `sites/`, `parameters/`, `templates/`
2. Update `.github/workflows/deploy.yaml` to add it to the `workspace` dropdown:

```yaml
workspace:
    description: "Workspace to deploy"
    required: true
    type: choice
    options:
        - iot-operations
        - iot-hub  # Add here
```

### Custom deployment workflow

Create a new workflow that calls the reusable workflow:

```yaml
name: Deploy My Service

on:
  push:
    branches: [main]
    paths: ['services/my-service/**']

jobs:
  deploy:
    uses: ./.github/workflows/_siteops-deploy.yaml
    with:
      manifest: manifests/my-service.yaml
      environment: dev
    secrets: inherit
```

### Setup Site Ops action

The `setup-siteops` composite action installs Python and Site Ops:

| Input | Default | Description |
|-------|---------|-------------|
| `python-version` | `3.11` | Python version to install |
| `install-dev` | `false` | Include dev dependencies (pytest, pytest-cov) |

Example with dev dependencies (for running tests):

```yaml
- uses: ./.github/actions/setup-siteops
  with:
    install-dev: "true"
```
