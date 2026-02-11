# GitHub Workflows Setup Guide

This guide explains how to configure GitHub Actions for siteops deployments.

## Prerequisites

1. Azure subscription with resources to deploy
2. GitHub repository with Actions enabled
3. Azure AD application for OIDC authentication

## Workflows Overview

| Workflow | Trigger | Purpose |
|----------|---------|---------|
| `validate-pr.yaml` | Pull request, manual | Run unit tests and validate manifests |
| `deploy.yaml` | Manual (workflow_dispatch) | Deploy infrastructure to Azure |
| `_siteops-deploy.yaml` | Called by deploy.yaml | Reusable deployment logic |

## Setup Azure OIDC Federation

OIDC (OpenID Connect) allows GitHub Actions to authenticate to Azure without storing secrets.

### 1. Create Azure AD Application

```bash
# Create app registration
az ad app create --display-name "github-siteops-deploy"

# Note the appId (client ID) from output
APP_ID=$(az ad app list --display-name "github-siteops-deploy" --query "[0].appId" -o tsv)

# Create service principal
az ad sp create --id $APP_ID

# Assign Contributor role (adjust scope as needed)
az role assignment create \
  --assignee $APP_ID \
  --role "Contributor" \
  --scope "/subscriptions/<SUBSCRIPTION_ID>"
```

### 2. Configure Federated Credentials

```bash
# For main branch deployments
az ad app federated-credential create \
  --id $APP_ID \
  --parameters '{
    "name": "github-main",
    "issuer": "https://token.actions.githubusercontent.com",
    "subject": "repo:<OWNER>/<REPO>:ref:refs/heads/main",
    "audiences": ["api://AzureADTokenExchange"]
  }'

# For environment-based deployments (recommended)
for ENV in dev staging prod; do
  az ad app federated-credential create \
    --id $APP_ID \
    --parameters "{
      \"name\": \"github-env-$ENV\",
      \"issuer\": \"https://token.actions.githubusercontent.com\",
      \"subject\": \"repo:<OWNER>/<REPO>:environment:$ENV\",
      \"audiences\": [\"api://AzureADTokenExchange\"]
    }"
done
```

### 3. Configure GitHub Secrets

Go to **Settings → Secrets and variables → Actions** and add:

| Secret | Value | Required |
|--------|-------|----------|
| `AZURE_CLIENT_ID` | Application (client) ID | Yes |
| `AZURE_TENANT_ID` | Directory (tenant) ID | Yes |
| `AZURE_SUBSCRIPTION_ID` | Azure subscription ID | Yes |
| `SITE_OVERRIDES` | JSON object with site overrides (see below) | No |

#### SITE_OVERRIDES Format

Override subscription, resource group, and parameters per site for CI deployments.
Supports nested paths using dot notation:

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

> **Note:** `SITE_OVERRIDES` is stored as a secret for access control (admin-only modification),
> not confidentiality. Subscription IDs and resource group names appear in deployment logs.

### 4. Configure GitHub Environments

Go to **Settings → Environments** and create:

#### `dev` Environment

- No protection rules (deploys immediately)

#### `staging` Environment  

- Required reviewers: 1 person
- Deployment branches: `main` only

#### `prod` Environment

- Required reviewers: 2 people
- Deployment branches: `main` only
- Wait timer: 5 minutes (optional)

## Usage

### Validate PR (Unit Tests + Manifest Validation)

Runs automatically on PRs that modify:

- `workspaces/**`
- `templates/**`
- `siteops/**`
- `tests/**`
- `pyproject.toml`

Can also be triggered manually from **Actions → Validate PR → Run workflow**.

### Deploy via GitHub UI

1. Go to **Actions** tab
2. Select **"Deploy Infrastructure"**
3. Click **"Run workflow"**
4. Fill in options:
   - **Git ref**: Branch, tag, or commit (optional)
   - **Workspace**: Directory containing sites/manifests (default: `workspaces/iot-operations`)
   - **Manifest**: Manifest to deploy (default: `aio-install`)
   - **Environment**: `dev`, `staging`, or `prod`
   - **Selector**: Additional site filter (optional, e.g., `region=eastus`)
   - **Dry run**: Preview only, no actual deployment
5. Click **"Run workflow"**

### Deploy via API

```bash
curl -X POST \
  -H "Authorization: token $GITHUB_TOKEN" \
  -H "Accept: application/vnd.github.v3+json" \
  https://api.github.com/repos/<OWNER>/<REPO>/actions/workflows/deploy.yaml/dispatches \
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

### Deploy via GitHub CLI

```bash
gh workflow run deploy.yaml \
  -f manifest="aio-install" \
  -f environment="dev" \
  -f dry-run="true"
```

## Demo Scenarios

The iot-operations workspace demonstrates key SiteOps capabilities:

| Step | Manifest | Environment | Sites | Demonstrates |
|------|----------|-------------|-------|--------------|
| 1 | `aio-install` | `staging` | chicago-staging | Base AIO platform only |
| 2 | `aio-install` | `dev` | munich-dev, seattle-dev | Parallel deployment + simulator |
| 3 | `aio-install` | `prod` | munich-prod, seattle-prod | Parallel deployment, no simulator |
| 4 | `opc-ua-solution` | `staging` | chicago-staging | Solution layer on existing AIO |

### Site Configuration

| Site | Environment | `deploySolution` | `enableOpcPlcSimulator` |
|------|-------------|------------------|-------------------------|
| munich-dev | dev | ✅ | ✅ |
| seattle-dev | dev | ✅ | ✅ |
| munich-prod | prod | ✅ | ❌ |
| seattle-prod | prod | ✅ | ❌ |
| chicago-staging | staging | ❌ | ❌ |

### Running the Demo

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
│     deploy.yaml         │   │      validate-pr.yaml       │
│  (workflow_dispatch)    │   │  (pull_request + manual)    │
└───────────┬─────────────┘   ├─────────────────────────────┤
            │                 │  • Unit Tests               │
            │                 │  • Manifest Validation      │
            │                 │  • Deployment Plan Preview  │
            ▼                 └─────────────────────────────┘
┌─────────────────────────────────────────────────────────────┐
│               _siteops-deploy.yaml (reusable)               │
├─────────────────────────────────────────────────────────────┤
│  1. Setup Site Ops                                          │
│  2. Generate sites.local/ from SITE_OVERRIDES secret        │
│  3. Show target sites (with overrides applied)              │
│  4. Azure Login (OIDC)                                      │
│  5. Start OIDC token refresh service (background)           │
│  6. Validate inputs (path traversal protection)             │
│  7. Run: siteops validate -v (validates + shows plan)       │
│  8. Run: siteops deploy                                     │
│  9. Upload deployment logs                                  │
│ 10. Azure Logout                                            │
└─────────────────────────────────────────────────────────────┘
```

## Security Features

| Feature | Description |
|---------|-------------|
| **OIDC Authentication** | No stored Azure credentials; tokens are short-lived |
| **Environment Protection** | Required approvals for staging/prod |
| **Input Validation** | Prevents path traversal and injection attacks |
| **Site Name Sanitization** | SITE_OVERRIDES keys validated against `^[a-zA-Z0-9_-]+$` |
| **Concurrency Control** | One deployment per environment at a time |
| **Least Privilege Permissions** | Workflows request minimal GitHub token scopes |
| **Token Refresh** | Background service refreshes OIDC token for long deployments |
| **Audit Trail** | All runs logged with triggering user |
| **Artifact Retention** | Deployment logs kept 30 days |

### Security Model

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

### Adding New Manifests

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

### Adding New Workspaces

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

### Custom Deployment Workflow

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

### Setup Site Ops Action

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
