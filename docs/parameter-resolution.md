# Parameter Resolution

Parameters flow from multiple sources and are automatically filtered per template.

## Merge order

| Priority | Source | Description |
|----------|--------|-------------|
| 1 (lowest) | Site parameters | `site.parameters` section |
| 2 | Manifest parameters | `manifest.parameters` list |
| 3 (highest) | Step parameters | `step.parameters` list |

Later values override earlier values. Nested objects merge recursively.

## Template variables

| Variable | Example |
|----------|---------|
| `{{ site.name }}` | `munich-dev` |
| `{{ site.location }}` | `germanywestcentral` |
| `{{ site.resourceGroup }}` | `rg-iot-munich-dev` |
| `{{ site.subscription }}` | `00000000-...` |
| `{{ site.labels.X }}` | Any label value |
| `{{ site.properties.X.Y }}` | Nested property |
| `{{ site.properties.X[0] }}` | Array indexing |
| `{{ steps.X.outputs.Y }}` | Output from step X |

## Output chaining

Reference outputs from previous steps:

```yaml
# parameters/chaining.yaml
schemaRegistryId: "{{ steps.schema-registry.outputs.schemaRegistry.id }}"
clExtensionIds: "{{ steps.aio-enablement.outputs.clExtensionIds }}"
```

> **Note**: Output chaining only works during real deployments. In `--dry-run` mode, output templates remain unresolved.

### Cross-scope output chaining

RG-level sites can reference outputs from subscription-scoped steps. Subscription outputs are keyed by subscription ID and resolved automatically:

```yaml
# parameters/chaining.yaml
edgeSiteId: "{{ steps.edge-site.outputs.site.id }}"
```

For `munich-line-1` (subscription: sub-123):
→ Resolves from subscription outputs for sub-123

For `munich-line-2` (subscription: sub-123):
→ Resolves from the same subscription outputs

**Resolution priority:**
1. Per-site step outputs (from RG-scoped steps)
2. Subscription outputs (from subscription-scoped steps, matched by site's subscription)

## Auto-filtering

Parameters are automatically filtered to only include values accepted by each template. This enables shared parameter files:

```yaml
# parameters/common.yaml - works with ANY template
location: "{{ site.location }}"
customLocationName: "{{ site.name }}-cl"
aioInstanceName: "{{ site.name }}-aio"
schemaRegistryName: "{{ site.name }}-sr"
adrNamespaceName: "{{ site.name }}-ns"
tags:
  environment: "{{ site.labels.environment }}"
```

When deploying:

- **schema-registry template**: Receives `location`, `tags`, `schemaRegistryName`
- **aio-instance template**: Receives `location`, `tags`, `customLocationName`, `aioInstanceName`
- Extra parameters are silently filtered out

## Best practices

| Parameter type | Where to define |
|----------------|-----------------|
| Site-specific sizing (replicas, memory) | `site.parameters` |
| Derived from site variables | Manifest-level `parameters/common.yaml` |
| Output chaining | Step-level `parameters/chaining.yaml` |
