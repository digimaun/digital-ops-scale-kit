# CI/CD Setup

This guide covers GitHub Actions configuration for automated deployments.

## Workflows

| Workflow | Purpose | Trigger |
|----------|---------|---------|
| `deploy.yaml` | Manual deployment | `workflow_dispatch` |
| `ci.yaml` | CI validation | Push, pull request |
| `_siteops-deploy.yaml` | Reusable deployment | Called by other workflows |

## Azure OIDC Configuration

Site Ops uses OIDC federation—no stored Azure credentials. Examples use bash syntax.

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

Create credentials for each environment (`dev`, `staging`, `prod`). Alternatively, configure the subject to match a branch (`ref:refs/heads/main`), pull request (`pull_request`), or tag (`ref:refs/tags/v*`) instead of an environment.

### 3. Assign Azure roles

For basic deployments, Contributor is sufficient:

```bash
az role assignment create \
  --assignee <app-id> \
  --role "Contributor" \
  --scope /subscriptions/<subscription-id>
```

**For AIO deployments:** The full installation includes RBAC operations (e.g., granting the AIO extension access to the schema registry). Contributor cannot create role assignments. Use Owner with a condition that prevents privilege escalation:

```bash
az role assignment create \
  --assignee <app-id> \
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
  -f workspace=iot-operations \
  -f manifest=aio-install \
  -f environment=dev
```

Add `-f selector="<value>"` to filter sites further:

- `selector="country=US"` — sites with country label
- `selector="name=seattle-dev"` — specific site by name
- `selector="country=US,name=seattle-dev"` — multiple filters

## Environments

Configure GitHub Environments for:

- Required reviewers (prod deployments)
- Environment-specific secrets
- Deployment protection rules
