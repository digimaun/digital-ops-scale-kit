// Delivers the AKS EE bootstrap launcher to an Arc-connected Windows VM via
// `Microsoft.HybridCompute/machines/runCommands`. The Connected Machine agent
// on the VM polls Azure, picks up the runCommand, executes the launcher
// script body locally with the supplied parameters, and reports back into
// the resource's instanceView.
//
// The launcher writes the worker + AKS EE config template to disk, creates a
// local admin user, registers a Scheduled Task running as that user, starts
// the task, and returns `REGISTERED`. ARM sees the runCommand succeed at
// that point. The actual bootstrap (Hyper-V enable, reboot, cluster deploy,
// Arc-connect, all 25-40 minutes of it) happens inside the Scheduled Task
// asynchronously. Pair this template with `runcommand-aksee-status.bicep`
// (separate runCommand that polls state.json) to gate downstream steps on
// bootstrap completion.
//
// Prerequisites on the target VM (one-time per VM, outside this Bicep):
//   1. Server is Arc-connected (e.g., via `OnboardingScript.ps1`).
//   2. SP referenced by `spAppId` has `Kubernetes Cluster - Azure Arc
//      Onboarding` (or broader Contributor) on the target resource group.
//      This SP is required by AKS Edge Essentials' install cmdlet to
//      register the new cluster with Arc as part of cluster creation.
//   3. The Arc machine's system-assigned identity has the same role.
//      Phase 3 (Arc operations after cluster create) authenticates as the
//      machine identity by default.
//
// Usage as a scalekit step:
//   - name: aksee-bootstrap
//     template: templates/host-bootstrap/aksee/template.bicep
//     scope: resourceGroup
//     parameters:
//       - parameters/inputs/aksee-bootstrap.yaml

@description('Name of the existing Arc-enabled Windows machine resource (Microsoft.HybridCompute/machines).')
param machineName string

@description('Name to assign the runCommands child resource. Use a stable name so re-deploys overwrite the existing command rather than accumulating history entries.')
param runCommandName string = 'aksee-bootstrap'

@description('Location for the runCommands resource. Defaults to the resource group location, which typically matches the machine location.')
param location string = resourceGroup().location

@description('Name of the Arc-connected Kubernetes cluster that AKS EE will register inside the worker. Must match the cluster name the scalekit site overlay expects.')
param clusterName string

@description('Resource group that holds the Arc-connected server and will receive the new connectedClusters resource. Typically the same RG that holds this runCommand.')
param targetResourceGroup string = resourceGroup().name

@description('Subscription ID where the cluster will be Arc-registered.')
param targetSubscription string = subscription().subscriptionId

@description('Azure region for the connectedClusters and custom-location resources the worker creates inside the VM.')
param targetLocation string = resourceGroup().location

@description('Azure AD tenant ID for the service principal.')
param tenantId string = subscription().tenantId

@description('Tenant-wide object ID for the Custom Locations RP service principal. Use `az ad sp show --id bc313c14-388c-4e7d-a58e-70017303ee3b --query id -o tsv` to retrieve.')
param customLocationsOid string

@description('Service principal application ID. Required for the fresh-VM bootstrap this Bicep delivers: AKS Edge Essentials demands SP credentials in its install cmdlet to create the cluster, and there is no flag to skip that step. Must be paired with spPassword. The pre-existing-cluster path (worker detects the cluster and short-circuits) is available via direct launcher invocation, not through this Bicep entry point.')
param spAppId string

@secure()
@description('Service principal client secret. Required; pair with spAppId. Marked @secure so Azure encrypts in transit and excludes from deployment history. The launcher encrypts at rest before writing to disk on the VM. NOTE: the value is passed as a CLI argument to the launcher by the Connected Machine Agent; values with special characters can break command-line parsing. Generate or rotate the SP secret to avoid characters outside [A-Za-z0-9._-].')
param spPassword string

@description('URL of the AKS Edge Essentials MSI to install. Default points at the official Microsoft latest-K3s aka.ms shortcut, which currently resolves to 1.12.269.0 / K3s 1.33.5. Override with a version-pinned URL only if you have a stable hosting URL you control. Microsoft does NOT publish stable per-version aka.ms links; the version path that appears in GitHub release notes is not a real download URL.')
param aksEdgeMsiUrl string = 'https://aka.ms/aks-edge/k3s-msi'

@description('When true, Phase 3 enables the OIDC issuer and workload identity on the Arc-connected cluster and patches the K3s apiserver `service-account-issuer`. Required only when downstream AIO components use workload-identity-backed secret sync. Defaults to false.')
param enableWorkloadIdentity bool = false

@description('Timeout for the runCommand in seconds. The launcher itself returns within ~30s after registering the Scheduled Task. The actual bootstrap runs asynchronously inside the task. 600s (10 min) covers worst-case slow file IO during launcher init without leaving ARM blocked unnecessarily.')
param runCommandTimeoutSeconds int = 600

resource machine 'Microsoft.HybridCompute/machines@2024-11-10-preview' existing = {
  name: machineName
}

resource bootstrapCommand 'Microsoft.HybridCompute/machines/runCommands@2024-11-10-preview' = {
  parent: machine
  name: runCommandName
  location: location
  properties: {
    source: {
      // loadTextContent inlines the launcher script body at compile time.
      // We use the MINIFIED launcher (comments + blank lines stripped,
      // leading whitespace removed) to stay under the empirical
      // Microsoft.HybridCompute runCommands size boundary. Microsoft
      // does not document an explicit limit; in practice the RP starts
      // returning HCRP413 around 38 KB raw script body (this workstream,
      // 2026-06-08: 37.6 KB worked, 38.8 KB rejected). JSON encoding
      // plus ARM envelope overhead amplify the wire-size, which is what
      // the RP actually checks. The full launcher (~53 KB raw) is
      // always over; the minified launcher (~34 KB raw today) stays
      // comfortably under. Long-term: switch to scriptUri delivery,
      // which has no documented limit but adds a storage dependency.
      script: loadTextContent('./scripts/Install-AksEeBootstrap.min.ps1')
    }
    // asyncExecution=false makes ARM block until the script body exits.
    // The launcher exits within ~30s (the long-running bootstrap is the
    // Scheduled Task it registers, which runs after ARM has already seen
    // success). Sync mode gives us a clean signal that the launcher itself
    // ran without error.
    asyncExecution: false
    timeoutInSeconds: runCommandTimeoutSeconds
    parameters: [
      { name: 'ClusterName',        value: clusterName }
      { name: 'ResourceGroup',      value: targetResourceGroup }
      { name: 'Subscription',       value: targetSubscription }
      { name: 'Location',           value: targetLocation }
      { name: 'TenantId',           value: tenantId }
      { name: 'CustomLocationsOid', value: customLocationsOid }
      { name: 'SpAppId',            value: spAppId }
      { name: 'AksEdgeMsiUrl',      value: aksEdgeMsiUrl }
      // The launcher param is [string]; string() yields 'true'/'false',
      // which the launcher parses case-insensitively. A bool value here
      // would be rejected by the runCommand's string-typed parameter.
      { name: 'EnableWorkloadIdentity', value: string(enableWorkloadIdentity) }
    ]
    protectedParameters: [
      // Azure encrypts these in transit and excludes them from any output
      // surfaced via instanceView.output. The launcher receives the value
      // as the -SpPassword parameter and encrypts it at rest via DPAPI
      // (LocalMachine scope) before writing to config.json.
      { name: 'SpPassword', value: spPassword }
    ]
  }
}

@description('Final execution state of the launcher script (typically `Succeeded` when the launcher registered the Scheduled Task without error). Independent of the actual bootstrap outcome, which the Scheduled Task drives asynchronously after this resource completes.')
output executionState string = bootstrapCommand.properties.instanceView.executionState

@description('Exit code from the launcher script. 0 = launcher returned REGISTERED. Non-zero = launcher failed before registering the Scheduled Task. Check `stdout` and `errorOutput` for diagnostics.')
output exitCode int = bootstrapCommand.properties.instanceView.exitCode

@description('Stdout captured from the launcher. Truncated by ARM, so blob upload is the alternative for large output. Typically contains the per-step launcher log lines and the final REGISTERED marker.')
output stdout string = bootstrapCommand.properties.instanceView.output

@description('Stderr captured from the launcher. Truncated by ARM. Typically empty on success, populated on launcher failure.')
output errorOutput string = bootstrapCommand.properties.instanceView.error

@description('Fully qualified resource ID of the Scheduled Task host (the Arc machine). Useful for chaining a downstream status-check runCommand against the same machine.')
output machineId string = machine.id

@description('Name of the runCommand resource. Re-deploys with the same name overwrite this resource; use a different name (e.g., timestamped) to keep history.')
output runCommandName string = bootstrapCommand.name
