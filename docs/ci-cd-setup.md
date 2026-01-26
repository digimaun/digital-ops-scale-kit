# CI/CD Setup

This guide covers GitHub Actions configuration for automated deployments.

## Workflows

| Workflow | Purpose | Trigger |
|----------|---------|---------|
| `deploy.yaml` | Manual deployment | `workflow_dispatch` |
| `validate-pr.yaml` | PR validation | Pull request |
| `_siteops-deploy.yaml` | Reusable deployment | Called by other workflows |

## Azure OIDC Configuration

Site Ops uses OIDC federation—no stored Azure credentials.

### 1. Create Azure AD application

```bash
az ad app create --display-name "siteops-github-actions"
```

### 2. Create federated credential

```bash
az ad app federated-credential create \
  --id <app-id> \
  --parameters '{
    "name": "github-main",
    "issuer": "https://token.actions.githubusercontent.com",
    "subject": "repo:YOUR-ORG/YOUR-REPO:environment:prod",
    "audiences": ["api://AzureADTokenExchange"]
  }'
```

Create credentials for each environment (`dev`, `staging`, `prod`).

### 3. Assign Azure roles

```bash
az role assignment create \
  --assignee <app-id> \
  --role "Contributor" \
  --scope /subscriptions/<subscription-id>
```

### Required secrets

| Secret | Required | Description |
|--------|----------|-------------|
| `AZURE_CLIENT_ID` | Yes | Azure AD application client ID |
| `AZURE_TENANT_ID` | Yes | Azure AD tenant ID |
| `AZURE_SUBSCRIPTION_ID` | Yes | Default subscription for OIDC login |
| `SITE_OVERRIDES` | No | JSON object with per-site overrides |

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

```json
{
  "munich-dev": {
    "subscription": "real-subscription-id",
    "resourceGroup": "real-resource-group",
    "parameters.clusterName": "real-cluster-name"
  },
  "seattle-dev": {
    "subscription": "real-subscription-id",
    "resourceGroup": "real-resource-group"
  }
}
```

Supports nested paths using dot notation (e.g., `parameters.clusterName`).

## Running Deployments

### Via GitHub UI

1. Go to **Actions** tab
2. Select **Deploy Infrastructure**
3. Click **Run workflow**
4. Select workspace, manifest, and environment

### Via GitHub CLI

```bash
gh workflow run deploy.yaml \
  -f workspace="iot-operations" \
  -f manifest="iot-ops-base" \
  -f environment="dev"
```

## Environments

Configure GitHub Environments for:

- Required reviewers (prod deployments)
- Environment-specific secrets
- Deployment protection rules
