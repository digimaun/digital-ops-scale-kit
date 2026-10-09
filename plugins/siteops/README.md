# Site Ops plugin for GitHub Copilot

Preview. This plugin teaches GitHub Copilot to deploy Azure IoT Operations
with Site Ops. It adds the `deploy-aio` skill, which installs AIO on an
existing cluster connected to Azure Arc, or enables Secret Sync on an
existing AIO instance. Copilot prepares the plan, shows it to you and
deploys only after you approve that plan.

The plugin uses the Agent Plugins 1.0 format, so other compatible clients
can load the same skill.

## Requirements

- Site Ops installed. See [Install Site Ops](../../docs/install-siteops.md).
- An approved content source, such as `siteops source enroll official`, and
  a release that declares typed inputs for the manifest.
- Azure CLI signed in with an identity that can read the cluster and deploy
  to its resource group.

## Install

From GitHub:

```text
copilot plugin install Azure/digital-ops-scale-kit:plugins/siteops
```

From a local checkout:

```text
copilot plugin install ./plugins/siteops
```

Start a new session and check that `/skills list` shows `deploy-aio`. After
editing a local copy, install it again to refresh the cached plugin.

## Use

Ask Copilot, for example:

- "Deploy AIO to my Arc cluster."
- "Install AIO with Secret Sync on plant-one."
- "Enable Secret Sync on my existing AIO instance."

Copilot asks for the source, release and target, shows the plan, and waits
for your approval before it runs `siteops deploy`.

Remove the plugin with `copilot plugin uninstall siteops`.
