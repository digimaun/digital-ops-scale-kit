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
- An approved content source, such as `siteops source enroll official`, with
  a release that declares typed inputs for the manifest. The skill proposes
  the newest such release of your source, preferring the official publisher
  when it has one, and you can choose another.
- Azure CLI signed in with an identity that can read the cluster and deploy
  to its resource group, and Git to list release tags.

## Install

Add the Scale Kit marketplace, then install the plugin from it:

```text
copilot plugin marketplace add Azure/digital-ops-scale-kit
copilot plugin install siteops@scale-kit
```

To try a branch, add the marketplace with that ref, for example
`copilot plugin marketplace add OWNER/REPO#BRANCH`. From a local checkout,
run `copilot plugin marketplace add .` at the repository root.

Start a new session and check that `/skills list` shows `deploy-aio`. A
local marketplace loads the plugin live, so edits apply in the next
session. For a marketplace on GitHub, run
`copilot plugin marketplace update scale-kit` and
`copilot plugin update siteops` to pick up changes.

## Use

Ask Copilot, for example:

- "Deploy AIO to my Arc cluster."
- "Install AIO with Secret Sync on plant-one."
- "Enable Secret Sync on my existing AIO instance."

Copilot asks for the source, release and target, shows the plan, and waits
for your approval before it runs `siteops deploy`.

Remove the plugin with `copilot plugin uninstall siteops`.
