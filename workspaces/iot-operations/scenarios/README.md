# Scenarios

Composed manifests that combine the AIO platform partial with optional capability partials and sample partials into a single end-to-end deployment artifact.

## Naming

`<base>-with-<addon>.yaml` for additive compositions. For example:

- `aio-with-opc-ua.yaml` (AIO platform + OPC UA sample)

## How a scenario is built

A scenario uses the `include:` directive (see `docs/manifest-includes.md`) to splice in:

- `_aio-fundamentals.yaml` (the AIO platform partial)
- `_resolve-aio.yaml` (when a downstream step needs to chain off the deployed instance)
- Sample partials (`samples/<name>/_partial.yaml`)

```yaml
# scenarios/aio-with-opc-ua.yaml
apiVersion: siteops/v1
kind: Manifest
name: aio-with-opc-ua
description: AIO platform + OPC UA sample.
selector: "environment=dev"
steps:
  - include: ../manifests/_aio-fundamentals.yaml
  - include: ../manifests/_resolve-aio.yaml
  - include: ../samples/opc-ua-solution/_partial.yaml
```

Omit `_resolve-aio.yaml` when the composition has no downstream consumer of the resolved instance/custom-location names. The OPC UA sample needs them, so it stays in.

## Composition rules

1. **Compose partials, not standalone manifests.** `manifests/aio-install.yaml` and `samples/<name>/manifest.yaml` are standalone entry points that re-include `_resolve-aio.yaml` so they can be deployed on their own. Composing two of them in one scenario will collide on the `resolve-aio` step name. Compose the underlying `_partial.yaml` files instead.
2. **Step names must be unique** across the post-include flat step list. Collision is a parse-time error.
3. **Site selectors and parallel settings** declared on a scenario apply at the scenario level. The same fields on included partials are silently ignored.

## Deployment

```bash
# Validate
siteops -w workspaces/iot-operations validate scenarios/aio-with-opc-ua.yaml -v

# Deploy to all matching sites
siteops -w workspaces/iot-operations deploy scenarios/aio-with-opc-ua.yaml -l environment=dev
```
