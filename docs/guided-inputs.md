# Supply one Site with typed inputs

Select a deployment, inspect its input contract, then prepare a plan for one
explicit Site. You can supply answers inline or in a file without adding a
Site to the workspace. The same manifest and planner still support configured
Sites and fleet selectors.

This guide uses `aio-install` in an approved, verified workspace package.
First [install a compatible Site Ops build](install-siteops.md) and
[pin that workspace](projects.md#run-project-pin). The package must
include `aio-install` and its input contract. The examples below use
`--approved-source official`, explicitly selecting the source enrolled
by the [bootstrap](install-siteops.md#choose-an-installation-route).
If you manage independent policy and trusted root files yourself,
use both `--trust-policy policy.json` and
`--trusted-root trusted-root.json` instead, and supply `--source`
when pinning. Never combine those options with an approved source.
The project pin cannot authorize its own source policy. A local
checkout selected with `-w` also works for authoring without a pin
or package trust options.

## Inspect and fill the inputs

Pin the approved content release for your project as described in
[operator projects](projects.md#use-an-approved-source). The following
commands use that project and enrollment:

```text
siteops --approved-source official --project ./factory inputs aio-install --example ./aio-inputs.yaml
```

This one command lists the required and defaulted inputs and writes the
example. Omit `--example` to inspect without writing anything. The
generated answer file has null values for the required answers. It
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
`--input`, before deploying. The default AIO path selects release 2608,
enables cert-manager, and leaves Secret Sync disabled.
Your existing cluster, resource group, appropriate Azure permissions, and
Azure CLI are prerequisites. Input resolution checks types and the Site
structure. It does not check that those Azure resources exist or that your
identity can deploy. Do not use a plan as evidence of cluster readiness.

Review the plan and target before deploying:

```text
siteops --approved-source official --project ./factory plan aio-install --input-file ./aio-inputs.yaml
siteops --approved-source official --project ./factory deploy aio-install --input-file ./aio-inputs.yaml
```

The ordinary plan performs local preflight and does not read Azure
resources or submit deployments.
The deploy command prepares again before making changes and may update
resources or incur charges. Authenticate explicitly with the identity
authorized for the target. A successful deployment result is not evidence of
AIO component health or a working application.

For an optional preview before planning, run
`siteops inputs aio-install --input-file ./aio-inputs.yaml` with the same
project and trust options.
It resolves and structurally validates one Site without writing it,
compiling templates or reading Azure resources. Plain and local JSON output
include a private display of the Site and its defaults. In CI and other
redacted destinations, the command reports resolution status without
publishing Site values.

## Use an existing resource ID

Alternatively, give the typed `cluster` input the full ARM ID of your
existing Arc-connected Kubernetes cluster. The generated answer file
keeps `subscription`, `resourceGroup`, `location` and `clusterName` as
`null`. Add `cluster` with your actual ID, then explicitly allow the
read with `--read-resources` on both `plan` and `deploy`:

```text
siteops --approved-source official --project ./factory plan aio-install --input-file ./aio-inputs.yaml --read-resources
siteops --approved-source official --project ./factory deploy aio-install --input-file ./aio-inputs.yaml --read-resources
```

For this route, also fill `siteName`, `environment` and `country`.
The ID must identify an existing
`Microsoft.Kubernetes/connectedClusters` resource. Site Ops checks its
type and identity, reads its region, and derives the four target values
before calling the normal planner. A supplied manual value must agree
with the observed resource. The read uses your existing Azure CLI
identity and fails if that identity cannot access the target. Site Ops
does not sign you in, change accounts or grant permissions. It reads
again for the separate deploy invocation rather than treating a
previous plan's observation as current.
`--offline` applies to the pinned content source only. It does not
prevent an Azure read explicitly requested with `--read-resources`.

To enable Secret Sync during that same AIO deployment, set
`enableSecretSync: true` in the answer file. This guided route requires
`cluster` and `--read-resources`. Before any deployment writes, Azure
must report an OIDC issuer and enabled workload identity on the existing
cluster. You may supply an optional `existingVault` resource ID when
enabled. It must be in the same subscription as the Site but may be in
a different resource group. Omit it to create a new vault. A successful
resource read establishes those reported settings at that moment, not
cluster readiness, federation success or secret materialization.

For a short non-secret command, supply the same named answers with repeated
`--input NAME=VALUE` options on `plan` or `deploy`. Inline answers override
the input file. Strings and strict `true` or `false` booleans are parsed
according to the selected contract. Do not put secrets in process arguments
or shell history. The initial typed route rejects contracts with protected
inputs. Duplicate and unknown answer names fail.

## Keep a Site for later

To retain the resolved configuration, create the project's `sites` directory
and run `inputs` with a completed answer file. When that file contains an Arc
cluster ID, authorize its resource read while saving:

```text
mkdir -p ./factory/sites
siteops --approved-source official --project ./factory inputs aio-install --input-file ./aio-inputs.yaml --read-resources --save-site ./factory/sites/plant-one.yaml
siteops --approved-source official --project ./factory plan aio-install -l name=plant-one
```

The answer file in this example must resolve `siteName: plant-one` and
its first cluster. The saved document is an ordinary Site. Site Ops does not overwrite an
existing file. An inline target remains in memory unless you explicitly
choose `--save-site`.
Saving a Site built from resource observations does not store the
observations or re-check their prerequisites on later `--site-file` use.
Keep Site and answer files outside the content cache and verified package.
To reuse a complete standalone Site without saving it into a project, pass
`--site-file ./plant-one.yaml` to `plan`, `validate`, or `deploy`. A standalone
Site must be complete and cannot inherit from packaged example Sites.
Configured Sites can continue to use the existing inheritance and overlay
rules.

For a separate fleet deployment after plant-one already runs AIO, reuse
its answer file only to prepare new Sites. Override the Site name and
cluster ID for each new target. Explicitly set `enableSecretSync=false`
on each new Site, even if the original answer file enabled it for
plant-one. Keep the four target fields derived from `cluster` as `null`
so each authorized read supplies the new cluster's facts:

```text
siteops --approved-source official --project ./factory inputs aio-install --input-file ./aio-inputs.yaml --input siteName=plant-two --input cluster="<second-Arc-cluster-ID>" --input enableSecretSync=false --read-resources --save-site ./factory/sites/plant-two.yaml
siteops --approved-source official --project ./factory inputs aio-install --input-file ./aio-inputs.yaml --input siteName=plant-three --input cluster="<third-Arc-cluster-ID>" --input enableSecretSync=false --read-resources --save-site ./factory/sites/plant-three.yaml
siteops --approved-source official --project ./factory plan aio-install -l name=plant-two,name=plant-three
```

The two-name selector bounds this plan to new Sites and excludes
plant-one. Review the target count and names before using the same
selector with `deploy`. Reapplying `aio-install` to a cluster that already
runs AIO can overwrite settings managed by the operator. To target three
or four new clusters instead, save more Sites with the same explicit
Secret Sync override and include only their names. The manifest permits
three concurrent Sites by default. For four concurrent Sites, pass
`--parallel 4` to both plan and deploy. Use a label such as
`environment=dev` only after confirming that it selects precisely the
new cohort and excludes plant-one. `parallel` limits concurrent work,
not the number of Sites selected. Saved Sites do not repeat guided
OIDC and workload identity checks when Secret Sync is enabled.

An explicit Site replaces the manifest's default selector or `sites:` list.
Combining `--site-file`, `--input-file`, or `--input` with `-l` fails rather than
joining another target. If an entry does not declare an input contract,
use a complete Site file or the configured-Site workflow. `browse` remains
descriptive: it never treats authored guidance as an executable input schema.

For fleet deployments, use [project Sites](projects.md) and
[targeting](targeting.md). For the separate permission and provenance
boundaries, see [workspace packages](workspace-packages.md).
