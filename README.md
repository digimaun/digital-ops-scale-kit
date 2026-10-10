# Digital Operations Scale Kit

**Azure infrastructure deployment at fleet scale.**

> [!NOTE]
> This project is under active development. If you're an Azure IoT Operations customer or interested in deployment at fleet scale, reach out at <aioteam@microsoft.com>.

Deploy Azure IoT Operations, or any Azure infrastructure, across dozens of Sites with a single command. Customization for each Site, parallel execution, and failure isolation are built in.

---

## Why Scale Kit?

Keep one deployment workflow for your fleet and change only what varies by
Site. Scale Kit combines reusable manifests, templates and parameters with
Site Ops, the CLI that runs them across your selected Sites.

- **Reuse the same manifest across Sites.** Compose templates and ordered
  steps once, then supply each Site's subscription, resource group, and settings.
- **Target the right part of your fleet.** Select one Site, an environment,
  or a labeled group with the same command.
- **Review before making changes.** Validate configuration and inspect a plan
  before submitting deployments.
- **See what happened at each Site.** Run Sites concurrently with failure
  isolation, and distinguish completed, failed, and skipped operations.

The same `plan` and `deploy` commands run locally and in CI/CD, where the
shipped pipelines select a workspace in your repository with `-w`. Site Ops
runs on demand, with no persistent orchestration service to operate.

## Scale Kit and Site Ops

**Scale Kit** provides the content: manifests, templates, parameters and
samples. Its included
IoT Operations workspace covers AIO installation, upgrades, workload resources,
and host lifecycle operations.

**Site Ops** is the reusable engine. It orchestrates your Bicep, ARM, kubectl,
and wait steps, so you can also use it with infrastructure beyond AIO.

A **Site** describes one deployment target and its settings. A **manifest**
describes the steps to run. A **workspace** groups those files with their
templates and parameters. Reusing a manifest across Sites keeps deployment
logic separate from the configuration of each environment.

## Quick start

Install Azure IoT Operations on one cluster connected to Azure Arc. You need
no repository clone, saved Site or project pin.

Before you start, have:

- A cluster connected to Azure Arc, and its full resource ID.
- Azure CLI 2.70 or newer, signed in with permission to deploy to that
  cluster's resource group.
- GitHub CLI 2.95 or newer from its
  [installation page](https://github.com/cli/cli#installation). Some
  distribution packages, such as Ubuntu 24.04's, are older. Site Ops uses it
  without a login to verify published content.
- `curl`, on Linux.

### 1. Install Site Ops

Open the release you want on the
[releases page](https://github.com/Azure/digital-ops-scale-kit/releases) and
copy one command from its **Install Site Ops** section. Each command is
generated for that exact release, so there is nothing to fill in.

| Your machine | Command in the release notes |
|---|---|
| Linux x64, including Ubuntu, Azure Cloud Shell and GitHub Codespaces | **Bootstrap without uv**, Linux. It checks the script against the notes, installs uv and Python for your user when needed, and authenticates the engine bundle before installing it. |
| Windows x64 | **Bootstrap without uv**, PowerShell. The same steps, without administrator rights. |
| uv already installed | **Already have uv**. uv installs the release wheel over HTTPS and its dependencies from your configured package index. |

A release that contains only content links to the engine release it uses.
The [installation guide](docs/install-siteops.md#choose-an-installation-route)
covers verifying the script before it runs, offline installation and
troubleshooting.

### 2. Approve the official content source

```text
siteops source enroll official
```

This approves releases that the official publisher builds from its main
branch, for 30 days. Site Ops deploys published content only from a source
you approve this way. Run it again to renew. See
[approved sources](docs/projects.md#use-an-approved-source) for other
publishers and custom policies.

### 3. Deploy AIO

Copy the deploy command from the release notes' **Workspace content**
section, which names the exact release, and replace the cluster placeholder:

```text
siteops deploy aio-install --source "official@<release>" --input "cluster=<Arc-cluster-resource-ID>"
```

Site Ops reads the cluster to fill in its subscription, resource group and
region. It uses your Azure CLI login and does not sign you in. It shows the
plan and asks before it changes anything. Pass `--yes` for unattended runs
such as CI. Deployment can incur charges. When it finishes,
[check the result](docs/guided-inputs.md#check-the-result).

To install Secret Sync as well, add `--input enableSecretSync=true`. To add
it to an instance that already runs AIO, use the
[standalone Secret Sync route](docs/guided-inputs.md#enable-secret-sync-on-an-existing-instance).
[Guided inputs](docs/guided-inputs.md) covers optional names, answer files
and separate plans.

### 4. Scale out on the same model

[Save Sites](docs/guided-inputs.md#keep-a-site-for-later) and pin a release
in an operator project, then plan and deploy to a selected group of Sites:

```text
siteops --approved-source official project pin ./factory --release <release>
siteops --approved-source official --project ./factory plan aio-install -l name=plant-two,name=plant-three
```

Confirm that the selector excludes clusters that already run AIO, because
reapplying `aio-install` can overwrite their settings. See
[project Sites](docs/projects.md), [fleet targeting](docs/targeting.md) and
the [local checkout guide](docs/getting-started.md) for fleet workflows and
content beyond AIO.

## Browse deployment choices

With a workspace pinned from an approved source, browse its packaged content
without a checkout:

```bash
siteops --approved-source official --project ./factory browse aio-install
```

The following `-w` commands require a local checkout. Inspect local
operations and samples before supplying deployment inputs:

```bash
siteops -w workspaces/iot-operations browse
siteops -w workspaces/iot-operations browse aio-install
siteops -w workspaces/iot-operations browse --tag mqtt
```

Browsing reads descriptions and authored guidance without loading Site values
or invoking deployment tools. Use the same name with `plan` and `deploy`,
or select the explicit path shown on the card. See [browse manifests](docs/browse-content.md)
for filters, private JSON and the journey from installation to workload.

Use `browse --source` to inspect a source's published index without cloning.
[Remote browsing](docs/remote-content.md) identifies the exact source revision
and links to its pinned guides. It does not acquire deployable workspace
content or grant deployment permissions.

## Learn and extend

| Task | Start here |
|---|---|
| Understand concepts and find a guide | [Documentation by task](docs/README.md) |
| Configure and target a fleet | [Sites](docs/site-configuration.md) and [targeting](docs/targeting.md) |
| Choose an AIO operation or sample | [IoT Operations workspace](workspaces/iot-operations/README.md) |
| Run deployments in automation | [CI/CD setup](docs/ci-cd-setup.md) |
| Build on the engine or contribute content | [Contributing](CONTRIBUTING.md) and [repository guide](docs/repository-guide.md) |

## License

[MIT](LICENSE)
