# E2E Testing

End-to-end (E2E) tests are the primary live-subscription validation for the scalekit. A single workflow run spins up a fresh k3s cluster, registers it with Azure Arc, deploys the full Azure IoT Operations stack via siteops, runs the integration tests, and tears everything down.

Use E2E tests when:

- Validating a PR that changes orchestration, merge, or deployment logic.
- Qualifying a new AIO version before updating workspace defaults.
- Reproducing a field issue end-to-end against a real subscription.

Unit tests (`pytest tests/ -m "not integration"`) cover every code path that does not require Azure and should remain the default pre-commit gate. E2E is intentionally opt-in (`workflow_dispatch`).

## How it fits together

```text
 ┌────────────────────────────────────────────────────────────┐
 │ GitHub workflow: e2e-test.yaml                             │
 │                                                            │
 │  prep  ──►  e2e (matrix over aio-versions)                 │
 │                  │                                         │
 │                  ├─ create-k3s-cluster  (composite action) │
 │                  ├─ azure/login         (OIDC)             │
 │                  ├─ connect-arc         (composite action) │
 │                  ├─ setup-siteops       (composite action) │
 │                  ├─ render-e2e-site.py  ──►  $RUNNER_TEMP/ │
 │                  │                           e2e-sites/    │
 │                  ├─ pytest tests/integration               │
 │                  │    (SITEOPS_EXTRA_SITES_DIRS points to  │
 │                  │     the rendered-site dir above)        │
 │                  └─ teardown (ephemeral: delete RG;        │
 │                               persistent: delta cleanup)   │
 └────────────────────────────────────────────────────────────┘
```

No Azure-specific site file is committed. The E2E site is rendered at run time from `tests/e2e/sites/e2e-test.yaml.tmpl` into a writable directory and surfaced to the orchestrator via `SITEOPS_EXTRA_SITES_DIRS` (see [Site configuration](site-configuration.md)).

## Modes

| Mode | Resource group | SP scope | When to use |
|------|---------------|----------|-------------|
| ephemeral (default) | Workflow creates and deletes per run. | Subscription-level `Owner`. | Routine CI validation, fully automated. |
| persistent | Operator supplies a pre-existing RG; only resources created during the run are deleted (snapshot delta). The cluster itself is always a fresh k3s on the runner (bring-your-own-cluster is not supported). | RG-level `Owner`. | Restricted subscriptions where sub-level Owner is not acceptable. Multi-version matrices are serialized in the shared RG. |

`Owner` is required (not `Contributor`) because AIO deployments make role assignments (for example, schema registry and Key Vault). `Contributor` cannot grant roles.

## Prerequisites

### 1. Azure and OIDC setup

Follow [CI/CD setup - Azure OIDC Configuration](ci-cd-setup.md#azure-oidc-configuration) to create the service principal and federated credential. The SP needs:

- **ephemeral mode:** `Owner` on the subscription.
- **persistent mode:** `Owner` on the target resource group.

### 2. Custom Locations RP object ID

`connect-arc` uses the Custom Locations RP principal object ID in the tenant. Grab it once per tenant:

```bash
az ad sp list --filter "displayname eq 'Custom Locations RP'" --query "[0].id" -o tsv
```

Pass the value as the `custom-locations-oid` workflow input, or set it as a repository/environment secret and wire it through (see inline workflow comments).

### 3. GitHub Environment and secrets

Create a GitHub Environment (for example, `dev`) and set these secrets:

| Secret | Source |
|--------|--------|
| `AZURE_CLIENT_ID` | App registration client ID |
| `AZURE_TENANT_ID` | `az account show --query tenantId -o tsv` |
| `AZURE_SUBSCRIPTION_ID` | `az account show --query id -o tsv` |

```bash
gh secret set AZURE_CLIENT_ID       --env dev --body "<app-client-id>"
gh secret set AZURE_TENANT_ID       --env dev --body "$(az account show --query tenantId -o tsv)"
gh secret set AZURE_SUBSCRIPTION_ID --env dev --body "$(az account show --query id -o tsv)"
```

## Running in CI

From the **Actions** tab, dispatch **E2E Tests** with the defaults to run a single-version ephemeral-mode pass against the `dev` environment:

| Input | Typical value | Notes |
|-------|--------------|-------|
| `aio-versions` | `2603` or `2603,2610` | Comma-separated. Ephemeral fans out in parallel; persistent serializes cells in the same RG. See [aio-versions.md](aio-versions.md) for how versions are defined and pinned. |
| `environment` | `dev` | GitHub Environment whose secrets/approvers apply. |
| `location` | `eastus2` | ephemeral mode only; persistent derives from the RG. |
| `resource-group` | empty (ephemeral) or existing RG (persistent) | |
| `cluster-name` | empty | Arc cluster name to register. auto-generated if empty. |
| `custom-locations-oid` | tenant value | See prerequisite 2. |
| `skip-teardown` | false | Preserve the deployment for inspection. Scope depends on mode (see below). |
| `keep-cluster-alive-minutes` | `0` | Hold the runner for N min before teardown for debugging. Max 300. Nothing should be added to the persistent RG during the hold (it'll be deleted by teardown). |

### What `skip-teardown` leaves behind

| Mode | Normal teardown | With `skip-teardown: true` |
|------|----------------|----------------------------|
| ephemeral | `az group delete` on the workflow-created RG. | **Entire RG and every resource inside it persist.** You are responsible for deleting the RG afterwards; otherwise orphan RGs accumulate and bill indefinitely. |
| persistent | `az connectedk8s delete` (only if the Arc cluster was created by this run) + snapshot-delta deletion of resources created during the run. RG itself is never touched. | Arc cluster + resources created by this run persist inside the operator's RG. Anything that existed before the run is untouched in either case. |

### Teardown safety guarantees

Ephemeral teardown runs three independent guards before `az group delete`. Any single mismatch hard-fails the step rather than proceeding:

1. **Name pattern.** RG must match `rg-e2e-<run_id>-<run_attempt>-*` built from the **current** workflow run, not a generic prefix. A pre-existing RG named `rg-e2e-...` from another run cannot pass.
2. **Tag provenance.** RG must carry `managedBy=siteops-e2e`, `ephemeral=true`, `run_id=<this run>`, `run_attempt=<this attempt>`. Tags are written by the `Create resource group` step and are never applied by the persistent path, so an operator-supplied RG cannot accidentally carry them.
3. **Existence.** A missing RG is treated as idempotent success (not failure), so reruns after manual cleanup do not fail spuriously.

Persistent teardown deletes the Arc cluster only if it was not present in the pre-run snapshot (i.e. only clusters this run registered; an operator-owned cluster with the same name is preserved). Resource deletion is bounded to the snapshot delta (post − pre): the workflow records every resource ID present in the RG before any Azure-side creation and deletes only what was added during the run. Missing snapshot → skip delta cleanup (manual inspection). Post-run enumeration failure → emit an error instead of declaring the RG clean.

**Use a dedicated RG for persistent mode.** Anything added to the RG between the snapshot and teardown — by operators, automation, or a `keep-cluster-alive-minutes` hold — appears in the delta and is deleted.

A JUnit XML artifact is uploaded per matrix cell (`e2e-results-<version>.xml`).

## Running locally

Local runs target your own k3s (or any Arc-connected) cluster against your own subscription. The renderer is cross-platform Python; no `envsubst` or bash required.

Set three required env vars; three more are auto-computed on first use.

| Variable | Required | Default |
|----------|----------|---------|
| `E2E_RESOURCE_GROUP` | yes | n/a |
| `E2E_CLUSTER_NAME` | yes | n/a |
| `E2E_AIO_VERSION` | yes | n/a |
| `E2E_SITE_NAME` | no | `e2e-local-<unix_time>` |
| `E2E_SUBSCRIPTION` | no | `az account show --query id -o tsv` |
| `E2E_LOCATION` | no | `az group show -n $E2E_RESOURCE_GROUP --query location -o tsv` |

### PowerShell (Windows)

```powershell
$env:E2E_RESOURCE_GROUP = "rg-e2e-dev"
$env:E2E_CLUSTER_NAME   = "arc-e2e-dev"
$env:E2E_AIO_VERSION    = "2603"
$env:E2E_SITE_NAME      = "e2e-local-$([DateTimeOffset]::Now.ToUnixTimeSeconds())"

$sitesDir = Join-Path $env:TEMP "e2e-sites"
python scripts/render-e2e-site.py --output-dir $sitesDir

$env:SITEOPS_EXTRA_SITES_DIRS = $sitesDir
$env:INTEGRATION_SELECTOR     = "name=$env:E2E_SITE_NAME"

pytest tests/integration/ -v -m integration
```

### bash (Linux / macOS / WSL)

```bash
export E2E_RESOURCE_GROUP=rg-e2e-dev
export E2E_CLUSTER_NAME=arc-e2e-dev
export E2E_AIO_VERSION=2603
export E2E_SITE_NAME="e2e-local-$(date +%s)"

SITES_DIR="${TMPDIR:-/tmp}/e2e-sites"
python scripts/render-e2e-site.py --output-dir "$SITES_DIR"

export SITEOPS_EXTRA_SITES_DIRS="$SITES_DIR"
export INTEGRATION_SELECTOR="name=$E2E_SITE_NAME"

pytest tests/integration/ -v -m integration
```

Setting `E2E_SITE_NAME` explicitly (or letting the renderer default to `e2e-local-<unix_time>`) gives you a predictable site name up front; the renderer also writes the file as `<E2E_SITE_NAME>.yaml` so the filename matches the site's `name:` field (the standard siteops convention).

You must already be logged in (`az login`) and have the cluster registered with Arc; the workflow automates these steps but local runs assume you already have an Arc-enabled target.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| `azure/login` fails with `AADSTS70021` | Federated credential `sub` claim mismatch. | Confirm the credential matches `repo:<org>/<repo>:environment:<env>` (or branch ref) exactly. See [CI/CD setup](ci-cd-setup.md#azure-oidc-configuration). |
| Pytest collects 0 integration tests | Selector does not match the rendered site, or `SITEOPS_EXTRA_SITES_DIRS` is unset. | Check `INTEGRATION_SELECTOR` equals the rendered site's `name:` field. |
| Rendered output still contains `${...}` | Template references a variable not in `ALL_VARS`. | Add it to `REQUIRED_VARS` or `OPTIONAL_VARS` in `scripts/render-e2e-site.py`. |
| AIO deploy fails with `AuthorizationFailed` on role assignment | SP is `Contributor`, not `Owner`. | Escalate to `Owner` on sub (ephemeral) or RG (persistent). |
| Persistent-mode teardown leaves resources | The snapshot step failed or was skipped. | Inspect the step summary warning and the `Snapshot RG resources` step log; clean up residual resources manually. |
| connect-arc times out waiting for `Connected` | OIDC issuer service is not reachable or Custom Locations RP object ID is wrong. | Verify prerequisite 2; re-run with `skip-teardown: true` and inspect `az connectedk8s show`. |

## Related docs

- [CI/CD setup](ci-cd-setup.md): OIDC, federated credential, general CI wiring.
- [Site configuration](site-configuration.md): trusted site directories and `SITEOPS_EXTRA_SITES_DIRS`.
- [Troubleshooting](troubleshooting.md): general siteops diagnostics.
