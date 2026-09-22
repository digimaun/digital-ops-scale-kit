# Supply one Site with typed inputs

Select a deployment, inspect its input contract, then prepare a plan for one
explicit Site. You can supply answers inline or in a file without adding a
Site to the workspace. The same manifest and planner still support configured
Sites and fleet selectors.

This guide uses `aio-install` in an approved, verified workspace package.
First [install a compatible Site Ops build](install-siteops.md) and
[pin that workspace](projects.md#run-project-pin) using an independently
provisioned verification policy and trusted root. The package must include
`aio-install` and its input contract. A local checkout selected with `-w`
also works for authoring, without the pin or package trust options.

## Inspect and fill the inputs

Replace the paths and release in the [project pin example](projects.md#run-project-pin)
with your approved source and policy. The following commands use that project:

```text
siteops --project ./factory --trust-policy policy.json --trusted-root trusted-root.json inputs aio-install
siteops --project ./factory --trust-policy policy.json --trusted-root trusted-root.json inputs aio-install --example ./aio-inputs.yaml
```

The generated answer file has null values for the required answers. It
cannot be used to plan or deploy until you supply every required value in
the file or override it with `--input`. For an existing Arc-connected
Kubernetes cluster, fill these fields with your own target information:

```yaml
apiVersion: siteops.inputs/v1
kind: SiteInputValues
values:
  siteName: null       # Choose a Site name, such as plant-one.
  subscription: null   # Use the subscription containing the Arc cluster.
  resourceGroup: null  # Use the existing cluster's resource group.
  location: null       # Use the cluster's Azure region.
  clusterName: null    # Use the existing Arc cluster name.
  environment: null    # Choose the environment label, such as dev or prod.
  country: null        # Choose the country label for resource tags.
```

Replace each null with a string value, or supply the same named answer with
`--input`, before deploying. The first AIO path
selects release 2608, enables cert-manager, and leaves Secret Sync disabled.
Your existing cluster, resource group, appropriate Azure permissions, and
Azure CLI are prerequisites. Input resolution checks types and the Site
structure. It does not check that those Azure resources exist or that your
identity can deploy. Do not use a plan as evidence of cluster readiness.

Review the plan and target before deploying:

```text
siteops --project ./factory --trust-policy policy.json --trusted-root trusted-root.json plan aio-install --input-file ./aio-inputs.yaml
siteops --project ./factory --trust-policy policy.json --trusted-root trusted-root.json deploy aio-install --input-file ./aio-inputs.yaml
```

The plan performs local preflight and does not submit Azure deployments.
The deploy command prepares again before making changes and may update
resources or incur charges. Authenticate explicitly with the identity
authorized for the target. A successful deployment result is not evidence of
AIO component health or a working application.

For a short non-secret command, supply the same named answers with repeated
`--input NAME=VALUE` options on `plan` or `deploy`. Inline answers override
the input file. Strings and strict `true` or `false` booleans are parsed
according to the selected contract. Do not put secrets in process arguments
or shell history. The initial typed route rejects contracts with protected
inputs. Duplicate and unknown answer names fail.

## Keep a Site for later

To retain the resolved configuration, create the project's `sites` directory
and run `inputs` with a completed answer file:

```text
siteops --project ./factory --trust-policy policy.json --trusted-root trusted-root.json inputs aio-install --input-file ./aio-inputs.yaml --save-site ./factory/sites/plant-one.yaml
siteops --project ./factory --trust-policy policy.json --trusted-root trusted-root.json plan aio-install -l name=plant-one
```

The saved document is an ordinary Site. Site Ops does not overwrite an
existing file. An
inline target remains in memory unless you explicitly choose `--save-site`.
Keep Site and answer files outside the content cache and verified package.
To reuse a complete standalone Site without saving it into a project, pass
`--site-file ./plant-one.yaml` to `plan`, `validate`, or `deploy`. A standalone
Site must be complete and cannot inherit from packaged example Sites.
Configured Sites can continue to use the existing inheritance and overlay
rules.

An explicit Site replaces the manifest's default selector or `sites:` list.
Combining `--site-file`, `--input-file`, or `--input` with `-l` fails rather than
joining another target. If an entry does not declare an input contract,
use a complete Site file or the configured-Site workflow. `browse` remains
descriptive: it never treats authored guidance as an executable input schema.

For fleet deployments, use [project Sites](projects.md) and
[targeting](targeting.md). For the separate permission and provenance
boundaries, see [workspace packages](workspace-packages.md).
