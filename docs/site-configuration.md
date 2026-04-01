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

> **Security**: Only base files (`sites/`) can specify `inherits`. Overlays cannot inject inheritance.

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

### SiteTemplate vs Site

| Aspect | `kind: Site` | `kind: SiteTemplate` |
|--------|--------------|----------------------|
| Can be deployed | ✅ Yes | ❌ No |
| Can be inherited from | ✅ Yes | ✅ Yes |
| Requires subscription/location | ✅ Yes | ❌ No |
| Discovered by `siteops sites` | ✅ Yes | ❌ No |

### Merge order with inheritance

`inherits target` → `sites/` → `sites.local/`

Inherited values are overridden by child site values. Nested objects (labels, parameters, properties) merge recursively.

> **Security**: Only base files (`sites/`) can specify `inherits`. Overlays cannot inject inheritance.
