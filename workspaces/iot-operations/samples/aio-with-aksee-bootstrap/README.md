# aio-with-aksee-bootstrap

Composes the AKS Edge Essentials host bootstrap with the AIO fundamentals install. Demonstrates the composed shape for bare-Arc-onboarded-Windows-VM to AIO.

## Two-stage today

The bootstrap step returns shortly after the Connected Machine Agent picks up the launcher (~90 seconds). The cluster then comes up on the VM asynchronously for 25 to 40 minutes. AIO fundamentals' first cluster-dependent step (`aio-enablement`) deploys an Arc extension that requires the `connectedClusters` resource to exist; until the bootstrap finishes, that resource is absent and `aio-enablement` fails with `ParentResourceNotFound`.

The deploy is therefore a two-stage flow today:

```bash
# Stage 1: dispatch the bootstrap. Expect failure partway through AIO fundamentals.
siteops -w workspaces/iot-operations deploy samples/aio-with-aksee-bootstrap/manifest.yaml -l environment=dev

# Monitor state.json on the VM via RDP until phase=99 status=succeeded (~25 to 40 min).
# See ../../templates/host-bootstrap/aksee/README.md for the monitor commands.

# Stage 2: re-run the same command. The bootstrap step short-circuits
# (cluster already deployed) and AIO fundamentals proceeds.
siteops -w workspaces/iot-operations deploy samples/aio-with-aksee-bootstrap/manifest.yaml -l environment=dev
```

## What this sample does

1. **aksee-bootstrap**: delivers and runs the bootstrap launcher on the target Windows VM via Arc Run Command. The launcher registers a Scheduled Task that drives a state machine through preflight, MSI install + Hyper-V enable (may reboot), single-node K3s cluster create, Arc-connect with custom locations, and cleanup. Survives the Hyper-V reboot via the at-startup task trigger and `state.json`.
2. **aio-fundamentals**: Arc extensions, custom location, AIO instance, schema registry, ADR namespace. Runs on the cluster the bootstrap produced.

After both stages complete, the cluster is registered with Arc, has custom locations enabled, and is running AIO. Add secret sync, OPC UA, or other workload samples on top via additional `include:` directives or by composing into a larger manifest.

## Prerequisites

The bootstrap prerequisites apply (Arc-onboarded VM, service principal with cluster Arc-onboarding rights, Arc machine identity with role on the resource group, resource providers registered). See [`../../templates/host-bootstrap/aksee/README.md`](../../templates/host-bootstrap/aksee/README.md) for the one-time setup walkthrough.

The site must carry both the `aksee` parameter section the bootstrap needs and any per-release AIO parameters the fundamentals expect (`properties.aioRelease` pointing at a file under `parameters/aio-releases/`).

## Variants

- **Bootstrap only:** `siteops deploy manifests/aksee-bootstrap.yaml` stops at "cluster Arc-connected + AIO-ready", without installing AIO.
- **Bootstrap + AIO + sample workload:** add another `include:` to a sample partial (e.g., `../opc-ua-solution/_partial.yaml`) to land an OPC UA solution on top.
- **Bootstrap + AIO + secret sync:** add `../../manifests/_resolve-aio.yaml` and `../../manifests/_secretsync.yaml` after the fundamentals step.
