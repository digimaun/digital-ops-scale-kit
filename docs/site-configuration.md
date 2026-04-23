# Site Configuration

Sites define **where** to deploy—the Azure subscription, resource group, location, and site-specific configuration.

## Site levels

Sites operate at two levels based on whether they have a `resourceGroup`:

| Site has | Site level | Deploys |
|----------|-----------|--------|
| `subscription` + `resourceGroup` | RG-level | Both subscription and RG-scoped steps |
| `subscription` only | Subscription-level | `scope: subscription` steps only |

**RG-level sites** are the most common—they deploy resources into a specific resource group.

**Subscription-level sites** deploy shared resources once per subscription (like Azure Edge Sites), then RG-level sites in that subscription can reference those outputs via cross-scope output chaining.

## Site structure

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

properties:
  deployOptions:
    includeSolution: true
```

**Subscription-level site** (for shared resources):

```yaml
apiVersion: siteops/v1
kind: Site
name: germany-subscription

subscription: "00000000-0000-0000-0000-000000000000"
location: germanywestcentral
# No resourceGroup → subscription-level site

labels:
  environment: dev

parameters:
  edgeSiteName: germany-edge-site
```

## Labels vs Parameters vs Properties

Sites have three ways to attach data, each serving a different purpose:

| Field | Data Type | Filtering | Conditionals | Template Access |
|-------|-----------|-----------|--------------|-----------------|
| `labels` | Flat strings only | ✅ `-l "key=value"` | ✅ `when:` | `{{ site.labels.X }}` |
| `parameters` | Any structure | ❌ | ❌ | `{{ site.parameters.X }}` |
| `properties` | Any structure | ❌ | ✅ `when:` | `{{ site.properties.X.Y }}` |

### Labels

Simple strings for **filtering** and **conditionals**:

```yaml
labels:
  environment: prod        # Filter: siteops deploy -l "environment=prod"
  city: Seattle            # Template variable: {{ site.labels.city }}
```

Use labels when you need to:

- Select sites with `-l` / `--selector`
- Reference simple string values in templates

### Parameters

Values passed directly to **Bicep templates**:

```yaml
parameters:
  clusterName: arc-seattle-prod      # Infrastructure identifier
  brokerConfig:                      # Complex objects for Bicep
    memoryProfile: Medium
    frontendReplicas: 4
```

Use parameters for:

- Infrastructure configuration (cluster names, sizing)
- Values that vary per-site based on capacity
- Complex objects consumed by Bicep templates

### Properties

Structured **metadata** and **deployment options**:

```yaml
properties:
  deployOptions:                     # Control deployment behavior
    includeSolution: true
    includeOpcPlcSimulator: false
    enableSecretSync: false
  tags:
    costCenter: operations
    team: platform
  opcUaEndpoints:                    # Arrays of configuration
    - name: cnc-machine-1
      address: opc.tcp://10.1.1.100:4840
```

Use properties for:

- Deployment options (conditionals via `when:`)
- Azure resource tags
- Arrays of endpoints or devices
- Nested metadata structures

### Conditionals

Properties support conditionals with truthy syntax:

```yaml
# Truthy check (recommended for booleans)
when: "{{ site.properties.deployOptions.includeSolution }}"

# Explicit comparison (also supported)
when: "{{ site.properties.deployOptions.includeSolution == true }}"
when: "{{ site.labels.environment == 'prod' }}"
```

### Rule of thumb

- Need to filter sites? → **Labels** (strings only)
- Need in `when` conditionals? → **Labels** (string comparison) or **Properties** (truthy check)
- Goes into Bicep templates? → **Parameters**
- Structured metadata (tags, arrays, deployment options)? → **Properties**

## Site overlays

Sites support layered definitions for separating committed config from local/CI overrides:

```
sites/           # Base definitions (committed to git)
sites.local/     # Overrides (gitignored)
```

**Merge order**: `sites/` → `sites.local/` (later values override earlier)

```yaml
# sites/munich-dev.yaml (committed)
name: munich-dev
subscription: "00000000-0000-0000-0000-000000000000"  # Placeholder
resourceGroup: placeholder
location: germanywestcentral
```

```yaml
# sites.local/munich-dev.yaml (gitignored)
subscription: "real-subscription-id"
resourceGroup: real-resource-group
```

> **Security**: Only base files (in trusted site directories) can specify `inherits`. Overlays in `sites.local/` cannot inject inheritance.

## Extra trusted site directories

In addition to the workspace's `sites/` directory, Site Ops can search
one or more extra trusted directories for site files. Files in these
directories are treated exactly like files in `sites/`: they are
discoverable by `siteops sites`, they can declare `inherits`, and they
serve as valid base files for the inheritance chain.

Use cases include:

- **CI / end-to-end tests**: keep test-only sites out of `workspaces/*/sites/`
  (production config) and inject them only when the test workflow runs.
- **Cross-repo site libraries**: pull shared sites from another repository
  checked out alongside the workspace.
- **Blueprint catalogs**: keep opinionated site templates in a central
  location, pointed at from multiple workspaces.

Provide extra directories via the CLI or environment variable:

```bash
# Repeatable flag
siteops -w workspace --extra-sites-dir ./tests/e2e/sites sites

# Environment variable (os.pathsep-separated: ';' on Windows, ':' on Unix)
SITEOPS_EXTRA_SITES_DIRS=/path/to/lib-sites siteops -w workspace sites
```

When both are provided, the CLI flag wins and an INFO log records that
the env var was ignored.

**Merge order (full)**:

```
inherits target → sites/ → <extra dirs, in listed order> → sites.local/
```

Extras cannot collide with the workspace's own `sites/` or `sites.local/`
directories; the orchestrator rejects both at construction time.
Registering `sites.local/` as trusted is specifically refused because it
would let overlays inject inheritance and break the overlay security
invariant.

### Leaf sites must live at the top level

Discovery is **flat, not recursive**. Every trusted directory (`sites/`,
each extras dir, and `sites.local/`) is scanned at its top level only.
Subdirectories are reserved for inherit targets and are reachable only
through an explicit subpath in `inherits:`.

| Path | Kind | Discovered? |
|---|---|---|
| `sites/munich-prod.yaml` | `Site` | ✅ deployable |
| `sites/base-site.yaml` | `SiteTemplate` | ✅ as inherit target only |
| `sites/shared/usa-west.yaml` | `SiteTemplate` | ❌ reachable only via `inherits: shared/usa-west.yaml` |
| `sites/eu/munich.yaml` | `Site` | ❌ silently ignored; move to top level |

## Site inheritance

Sites can inherit from shared templates to reduce duplication:

```yaml
# sites/base-site.yaml
apiVersion: siteops/v1
kind: SiteTemplate
name: base-site

parameters:
  brokerConfig:
    memoryProfile: Medium
    frontendReplicas: 2

properties:
  tags:
    project: iot-operations
    managedBy: siteops
```

```yaml
# sites/munich-dev.yaml
apiVersion: siteops/v1
kind: Site
name: munich-dev
inherits: base-site.yaml

subscription: "00000000-0000-0000-0000-000000000000"
resourceGroup: rg-iot-munich-dev
location: germanywestcentral

labels:
  environment: dev

parameters:
  brokerConfig:
    memoryProfile: Low  # Overrides inherited value
```

### How `inherits:` paths are resolved

Resolution is relative to the **child file's own directory**. The only
exception is the bare-filename fallback (row 1 below), which lets an
extras-dir site inherit a workspace-owned template without copying it.

| Form | Example | Resolves to |
|---|---|---|
| Bare filename | `inherits: base-site.yaml` | `./base-site.yaml` next to the child, then fallback to `<workspace>/sites/base-site.yaml` |
| Subpath | `inherits: shared/usa-east.yaml` | `<child-dir>/shared/usa-east.yaml` |
| Parent / sibling | `inherits: ../base-site.yaml` | `<child-dir>/../base-site.yaml` |
| Absolute | `inherits: /abs/path/tpl.yaml` | Used as-is |

The fallback searches `<workspace>/sites/` only (never across extras
dirs), so there is no implicit shared-template namespace between trusted
directories.

> **Trust model.** `inherits:` is author-trusted and not filesystem-sandboxed;
> it may point to a sibling `shared/` dir or an absolute path. The control is
> *who may author files in trusted sites locations* (`workspace/sites/` and
> extras dirs); anyone who can write an `inherits:` value can already set any
> other site field. `sites.local/` overlays strip `inherits:`, so runtime
> overlays cannot introduce new inheritance targets.

### SiteTemplate vs Site

| Aspect | `kind: Site` | `kind: SiteTemplate` |
|--------|--------------|----------------------|
| Can be deployed | ✅ Yes | ❌ No |
| Can be inherited from | ✅ Yes | ✅ Yes |
| Requires subscription/location | ✅ Yes | ❌ No |
| Discovered by `siteops sites` | ✅ Yes | ❌ No |

### Merge order with inheritance

`inherits target` → `sites/` → `<extra trusted dirs>` → `sites.local/`

Inherited values are overridden by child site values. Nested objects (labels, parameters, properties) merge recursively. See [Extra trusted site directories](#extra-trusted-site-directories) for how extra dirs participate in the chain.

> **Security**: Only base files (in trusted site directories) can specify `inherits`. Overlays in `sites.local/` cannot inject inheritance, even when extra trusted dirs are configured.

## Site selection from a manifest

When a manifest is deployed, Site Ops resolves the target sites through
three mutually-exclusive precedence tiers:

1. **CLI `--selector` overrides everything.**
   ```bash
   siteops deploy manifests/aio-install.yaml --selector environment=dev
   siteops deploy manifests/aio-install.yaml --selector name=munich-dev
   ```
   - If the selector includes `name=X` and a trusted file `X.yaml` (or
     `X.yml`) exists, the named site is loaded directly; any load
     error (broken inherits chain, invalid YAML) is surfaced instead of
     silently resolving to zero sites.
   - Otherwise the orchestrator loads all discoverable sites and keeps
     those whose `name:` or `labels` match every `key=value` pair. This
     path supports selecting by the site's internal `name:` field even
     when it differs from the filename.

2. **Explicit `sites:` list in the manifest.**
   ```yaml
   # manifests/regional-rollout.yaml
   sites:
     - chicago-staging
     - seattle-prod
   ```
   Each entry is a filename stem; missing files raise
   `FileNotFoundError` with the full list.

3. **Manifest `siteSelector:` (label expression).**
   ```yaml
   # manifests/aio-install.yaml
   siteSelector: "environment=dev"
   ```
   The orchestrator loads all discoverable sites and keeps those whose
   labels satisfy the expression.

A manifest with none of the three is rejected at parse time.

### Quick decision table

| I want to… | Do this |
|---|---|
| Add a new deployable site | Drop `my-site.yaml` at the root of `workspace/sites/` or an extras dir |
| Share a reusable template across sites | Put it in `workspace/sites/<name>.yaml` (same dir) or `workspace/sites/shared/<name>.yaml` (subdir) and reference via `inherits:` |
| Override a committed site at runtime without a PR | Put `my-site.yaml` in `workspace/sites.local/` (overlay merges; `inherits:` stripped) |
| Inject a site from CI without touching the workspace | Register a dir via `SITEOPS_EXTRA_SITES_DIRS` / `--extra-sites-dir` and drop `my-site.yaml` at its root |
| Target one specific site at the CLI | `siteops deploy <manifest> --selector name=<site-name>` |
| Pin the manifest to a labeled cohort | Set `siteSelector:` in the manifest |
| Hard-code the target list for a manifest | Set `sites:` in the manifest |
