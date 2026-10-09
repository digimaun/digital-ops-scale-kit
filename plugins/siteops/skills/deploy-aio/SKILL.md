---
name: deploy-aio
description: Deploy Azure IoT Operations (AIO) to one or several existing clusters connected to Azure Arc, or enable Secret Sync on an existing AIO instance, using Site Ops. Use when the user asks to install, deploy or set up AIO or Secret Sync, on one cluster or a fleet.
---

# Deploy Azure IoT Operations with Site Ops

Site Ops (`siteops`) prepares a deployment plan, shows it and runs it. Your
job is to collect the few answers it needs, show the user the plan and run
the deployment only after the user approves that plan.

## Rules

- Deploy only after the user has seen the plan from step 5 and approved it
  explicitly in this conversation. Silence, approval of an earlier plan or a
  general instruction to proceed is not approval.
- Run `deploy` with exactly the manifest, source, release and inputs of the
  reviewed `plan`. If anything changes, prepare the plan again and ask again.
- Do not install tools, enroll a source, sign in to Azure or change Azure CLI
  or GitHub CLI settings unless the user asks you to. Explain what is missing
  and offer the documented command instead.
- Keep resource IDs, subscription IDs and tenant details in this
  conversation. Do not write them into commits, issues, pull requests or
  files the user did not ask for.
- Deployment creates or updates Azure resources and can incur charges.
  Deploying again can overwrite settings. Site Ops does not roll back or
  delete resources after a failure.

## 1. Check the tools

```text
siteops --version
az account show --query "{subscription:name, user:user.name}" --output table
```

If `siteops` is missing, point the user to the
[installation guide](https://github.com/Azure/digital-ops-scale-kit/blob/main/docs/install-siteops.md)
and ask before running an installer. If Azure CLI reports no account, ask
the user to run `az login` themselves. Site Ops also needs GitHub CLI 2.95
or newer, without a login, to verify published content.

## 2. Choose an approved source and release

```text
siteops source list
```

Use an approved source the user already has. If there is none, explain that
enrolling approves a publisher's releases for 30 days. With the user's
agreement, enroll the official publisher:

```text
siteops source enroll official
```

For another publisher the user names, such as a fork, add its repository:

```text
siteops source enroll <source> --source github:<owner>/<repository>
```

Unless the user names a release, propose the newest one that supports this
route. Site Ops deploys only an explicit release, so resolve it first:

1. Show the source's repository:

   ```text
   siteops source show <source>
   ```

2. List its release tags. This needs Git but no sign in:

   ```text
   git ls-remote --tags --refs https://github.com/<owner>/<repository>
   ```

3. Sort the tags that start with `v` by version, newest first, and check
   the newest with the manifest you will deploy:

   ```text
   siteops inputs aio-install --source "<source>@<release>"
   ```

   If Site Ops reports that the release has no such manifest or no typed
   inputs, check the next older tag. Stop after three and ask the user.

If the user has enrolled the official publisher
(`github:Azure/digital-ops-scale-kit`) and another source, prefer the
official one when its newest release passes step 3. Otherwise use the
other source. Tell the user which source and release you chose and why,
and let them choose another. If Site Ops reports that the installed engine
is not compatible with the release, point the user to that release's
installation instructions.

## 3. Identify the targets

AIO installation needs the full resource ID of each existing cluster
connected to Azure Arc:

```text
az resource list --resource-type Microsoft.Kubernetes/connectedClusters --query "[].id" --output tsv
```

Enabling Secret Sync on an existing AIO instance needs the instance resource
ID instead:

```text
az resource list --resource-type Microsoft.IoTOperations/instances --query "[].id" --output tsv
```

Confirm each exact ID with the user. Never choose between several yourself.
For several clusters, check that each is in its own resource group, because
AIO supports one instance per resource group.

## 4. Choose the route and inputs

| The user wants | Manifest | Required inputs |
|---|---|---|
| Install AIO on one cluster | `aio-install` | `cluster` |
| Install AIO and enable Secret Sync | `aio-install` | `cluster`, `enableSecretSync=true` |
| Enable Secret Sync on an existing AIO 2607 or 2608 instance | `secretsync` | `instance` |
| Install AIO on several clusters | `aio-install` | `cluster` and `siteName` for each, saved as Sites (step 5) |

The defaults are AIO release 2608, cert-manager enabled, Secret Sync
disabled and a Site name generated from the cluster. List every optional
input, such as `siteName`, `environment`, `aioRelease`, `existingVault` and
`brokerMemoryProfile`, with:

```text
siteops inputs aio-install --source "<source>@<release>"
```

Add an optional input only when the user asks for it. Combined Secret Sync
needs the cluster to report an OIDC issuer and workload identity. Site Ops
checks both before any write.

## 5. Prepare and show the plan

For one cluster:

```text
siteops plan aio-install --source "<source>@<release>" --input "cluster=<cluster-resource-ID>"
```

For Secret Sync on an existing instance:

```text
siteops plan secretsync --source "<source>@<release>" --input "instance=<instance-resource-ID>"
```

Add the same `--input` options you chose in step 4.

For several clusters, save one Site per cluster in a project, then plan
them together:

1. Agree with the user on a project directory, such as `./factory`, and a
   Site name per cluster, such as the cluster name. Site names use
   lowercase letters, digits and hyphens. Ask before creating files, then
   create the project's `sites` directory if it does not exist.
2. Save each Site. The cluster read fills in its subscription, resource
   group and region. Give every Site the same optional inputs:

   ```text
   siteops --project <project> inputs aio-install --source "<source>@<release>" --input "cluster=<cluster-resource-ID>" --input siteName=<site-name> --read-resources --save-site <project>/sites/<site-name>.yaml
   ```

   Site Ops never overwrites a file or reuses the name of a configured
   Site. If it refuses, ask the user rather than choosing another name.
3. Plan only the new Sites, selected by name:

   ```text
   siteops --project <project> plan aio-install --source "<source>@<release>" -l name=<site-name>,name=<other-site-name>
   ```

   The manifest deploys three Sites at once by default. For more, add
   `--parallel` with the Site count to both plan and deploy. Do not select
   by a shared label such as `environment=dev`. It can include clusters
   that already run AIO, and deploying `aio-install` again can overwrite
   their settings.

Planning reads the named resources with the user's Azure CLI identity but
changes nothing. Show the user each Site with its subscription, resource
group and AIO release, the Site count and the operations, then ask whether
to deploy this plan.

If planning fails, show the error, explain it with the
[troubleshooting guide](https://github.com/Azure/digital-ops-scale-kit/blob/main/docs/troubleshooting.md)
and correct the inputs with the user. Do not continue to deployment.

## 6. Deploy after approval

Only after the user approves the plan from step 5, run the same command with
`deploy` and `--yes`:

```text
siteops deploy aio-install --source "<source>@<release>" --input "cluster=<cluster-resource-ID>" --yes
```

```text
siteops deploy secretsync --source "<source>@<release>" --input "instance=<instance-resource-ID>" --yes
```

```text
siteops --project <project> deploy aio-install --source "<source>@<release>" -l name=<site-name>,name=<other-site-name> --yes
```

`--yes` carries the user's approval, because an agent shell cannot answer
the interactive confirmation. Deployment can take more than 20 minutes. Let
it finish and keep the user informed. If it is interrupted, work that Azure
already accepted continues. Tell the user, and do not start another
deployment without new approval.

## 7. Report the outcome

Summarize the result line and any failed operation, for each Site in a
fleet. A successful result means the deployment operations completed, not
that AIO is healthy. Offer the AIO health check, which needs a kubeconfig
context for each cluster:

```text
az iot ops check --context "<kubeconfig-context>"
```

For Secret Sync, a successful deployment does not prove that a secret
reached the cluster. See the
[Secret Sync reference](https://github.com/Azure/digital-ops-scale-kit/blob/main/docs/secret-sync.md)
for functional checks.

## References

- [Deploy AIO with guided inputs](https://github.com/Azure/digital-ops-scale-kit/blob/main/docs/guided-inputs.md)
- [Approved sources](https://github.com/Azure/digital-ops-scale-kit/blob/main/docs/projects.md#use-an-approved-source)
- [Troubleshooting](https://github.com/Azure/digital-ops-scale-kit/blob/main/docs/troubleshooting.md)
