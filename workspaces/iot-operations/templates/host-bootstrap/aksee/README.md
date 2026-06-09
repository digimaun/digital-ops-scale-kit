# AKS Edge Essentials host bootstrap

Takes a freshly Arc-onboarded Windows VM through:

- AKS Edge Essentials install (MSI + Hyper-V, including the reboot)
- Single-node K3s cluster deployment
- Cluster Arc-connect with custom locations enabled (OIDC issuer + workload identity opt-in)

After this completes, the cluster satisfies the AIO prerequisites and the existing AIO deploy chain runs against it.

Delivered remotely from Azure via Arc Run Command. The launcher writes a worker state machine + supporting files to the VM, registers a Scheduled Task that drives the worker through all phases (including survival of the Hyper-V reboot), and returns once the task is started. The operator never RDPs to the VM.

## How it composes

Three entry shapes:

| Entry | Use |
|---|---|
| `manifests/aksee-bootstrap.yaml` | Standalone host bootstrap. Stops at "cluster Arc-connected + AIO-ready". |
| `templates/host-bootstrap/aksee/_partial.yaml` | Internal partial co-located with this implementation. Composed by the standalone above and by compositions like the next row. |
| `samples/aio-with-aksee-bootstrap/manifest.yaml` | End-to-end bare VM to AIO in one deploy. Composes the partial above plus `_aio-fundamentals.yaml`. |

## Prerequisites per target VM (one-time)

### 1. Server is Arc-connected

The VM must already be Arc-onboarded as a `Microsoft.HybridCompute/machines` resource before the bootstrap runs. Use the [official onboarding flow](https://learn.microsoft.com/azure/azure-arc/servers/onboard-portal) or your existing onboarding script. The bootstrap targets the VM by its Arc machine name.

### 2. Service principal with cluster Arc-onboarding rights

AKS Edge Essentials' install cmdlet requires a service principal to register the new cluster with Arc as part of cluster create. The SP needs `Kubernetes Cluster - Azure Arc Onboarding` (or broader Contributor) on the target resource group.

```bash
az ad sp create-for-rbac --name "aksee-bootstrap-sp" --role "Kubernetes Cluster - Azure Arc Onboarding" --scopes "/subscriptions/<sub>/resourceGroups/<rg>"
```

Save the `appId` and `password` returned. Pin or rotate the secret to characters in `[A-Za-z0-9._-]` (the secret is passed as a CLI argument to the launcher by the Connected Machine Agent; characters outside that range can break command-line parsing).

### 3. Grant the Arc machine identity access to the resource group

The Arc machine's system-assigned identity authenticates the AIO-specific Arc operations after cluster create (`az connectedk8s enable-features`, OIDC issuer wiring when workload identity is requested). It needs the same role on the resource group.

```bash
ARC_PRINCIPAL_ID=$(az resource show -g <rg> -n <vm-name> --resource-type Microsoft.HybridCompute/machines --query "identity.principalId" -o tsv)
az role assignment create --assignee-object-id $ARC_PRINCIPAL_ID --assignee-principal-type ServicePrincipal --role "Contributor" --scope "/subscriptions/<sub>/resourceGroups/<rg>"
```

### 4. Resource providers registered on the subscription

```bash
az provider register --namespace Microsoft.HybridCompute
az provider register --namespace Microsoft.Kubernetes
az provider register --namespace Microsoft.KubernetesConfiguration
az provider register --namespace Microsoft.ExtendedLocation
az provider register --namespace Microsoft.IoTOperations
```

## Site configuration

Add an `aksee` section under your site's `parameters`. Split the secret from the rest so the secret stays out of the committable `sites/` tree:

```yaml
# sites/<site>.yaml (committable, no secrets)
name: my-site
subscription: <subscription-id>
resourceGroup: <rg-name>
location: westus2
labels:
  environment: dev
parameters:
  aksee:
    machineName: my-arc-windows-vm
    clusterName: my-aksee-cluster
    customLocationsOid: <custom-locations RP object id>
    spAppId: <SP application id>
```

```yaml
# sites.local/<site>.yaml (gitignored, holds the SP secret)
name: my-site
parameters:
  aksee:
    spPassword: <SP client secret>
```

Required fields:

| Field | Source |
|---|---|
| `machineName` | The Arc-onboarded VM's machine resource name. |
| `clusterName` | Name to register the new K3s cluster as in Arc. New per site. |
| `customLocationsOid` | `az ad sp show --id bc313c14-388c-4e7d-a58e-70017303ee3b --query id -o tsv`. Tenant-wide. |
| `spAppId` | The service principal created in prereq #2. |
| `spPassword` | The service principal secret. Source from a CI Key Vault binding in production. |

## Run

```bash
# Standalone host bootstrap (stops at cluster Arc-connected + AIO-ready)
siteops -w workspaces/iot-operations deploy manifests/aksee-bootstrap.yaml -l environment=dev

# Or bootstrap + AIO install in one deploy
siteops -w workspaces/iot-operations deploy samples/aio-with-aksee-bootstrap/manifest.yaml -l environment=dev
```

The deploy returns the moment the launcher returns `REGISTERED` (typically 30 to 90 seconds after the Arc agent picks up the run command). The actual bootstrap (25 to 40 minutes wall time) runs asynchronously on the VM inside the Scheduled Task. Use the monitor commands below from RDP to track phase progression.

## Monitor

From an admin PowerShell session on the VM (non-admin sessions cannot read the working directory, which is ACL-locked to Administrators + SYSTEM):

```powershell
$dir = 'C:\ProgramData\siteops\aksee-bootstrap'

# State (re-run every 30 to 60 seconds)
Get-Content (Join-Path $dir 'state.json') | ConvertFrom-Json | Format-List

# Worker log tail
$log = Get-ChildItem (Join-Path $dir 'worker-*.log') | Sort-Object LastWriteTime | Select-Object -Last 1
if ($log) { Get-Content $log.FullName -Tail 30 }
```

Phase progression to expect:

| Phase | Status | What's happening |
|---|---|---|
| 0 | running | Pre-flight checks (admin, OS, memory, disk, NuGet provider) |
| 1 | running | MSI install, Hyper-V enable (may reboot) |
| 2 | pending-reboot | Hyper-V reboot imminent or in progress |
| 2 | running | Cluster deployment (10 to 15 minutes) |
| 3 | running | Azure CLI install, Arc operations, custom locations enablement (5 to 10 minutes) |
| 99 | succeeded | Done |

Live-follow form for the latest worker log:

```powershell
$log = Get-ChildItem 'C:\ProgramData\siteops\aksee-bootstrap\worker-*.log' | Sort-Object LastWriteTime | Select-Object -Last 1
Get-Content $log.FullName -Tail 50 -Wait
```

## Verify

On the VM after `state.json` shows `phase=99 status=succeeded`. The bootstrap writes the kubeconfig under the bootstrap user's profile (which Phase 99 removes) and copies it to the shared ACL-locked path below. Open an admin PowerShell and point `KUBECONFIG` at the shared copy:

```powershell
$env:KUBECONFIG = 'C:\ProgramData\siteops\aksee-bootstrap\kubeconfig'

kubectl get nodes
# Expect: one node, status Ready

az connectedk8s show --name <cluster-name> --resource-group <rg> --query connectivityStatus
# Expect: Connected
```

If you bootstrapped with workload identity enabled (see "Optional flags" below), additionally verify the OIDC issuer and workload identity surface:

```powershell
az connectedk8s show --name <cluster-name> --resource-group <rg> --query "{oidc:oidcIssuerProfile.enabled, wi:securityProfile.workloadIdentity.enabled}"
# Expect: oidc=true, wi=true
```

## If something goes wrong

### Monitoring shells need admin

`C:\ProgramData\siteops\aksee-bootstrap\` has ACLs locked to Administrators + SYSTEM at launcher time (the directory holds the encrypted SP secret). A non-admin PowerShell session cannot read the state file, the worker transcripts, or the msiexec log. Open monitoring shells as Administrator.

### Bootstrap stuck on a phase

```powershell
# Read the error from state.json
$state = Get-Content 'C:\ProgramData\siteops\aksee-bootstrap\state.json' | ConvertFrom-Json
$state.error

# Read the latest transcript
$log = Get-ChildItem 'C:\ProgramData\siteops\aksee-bootstrap\worker-*.log' | Sort-Object LastWriteTime | Select-Object -Last 1
Get-Content $log.FullName -Tail 100

# MSI install errors (Phase 1)
Get-Content 'C:\ProgramData\siteops\aksee-bootstrap\msiexec.log' -Tail 100

# AKS EE deployment errors (Phase 2). Worker captures the cmdlet's stdout
# and stderr from the child PowerShell process to separate files.
Get-ChildItem 'C:\ProgramData\siteops\aksee-bootstrap\aksee-deploy-*.log*' | Sort-Object LastWriteTime | Select-Object -Last 2 | ForEach-Object {
    Write-Host "===== $($_.Name) ====="
    Get-Content $_.FullName -Tail 20
}
```

### Scheduled Task is not firing

```powershell
Get-ScheduledTask -TaskName SiteOpsAksEeBootstrap | Get-ScheduledTaskInfo
# Look at LastRunTime and LastTaskResult. Result 0 = success.

Get-WinEvent -LogName 'Microsoft-Windows-TaskScheduler/Operational' -MaxEvents 50 |
    Where-Object { $_.Message -like '*SiteOpsAksEeBootstrap*' }
```

### Re-run a failed phase (keeps existing task and user)

Use when a transient failure hit a single phase (network blip, az CLI download timeout) and you want to retry the same phase without re-running the launcher.

```powershell
# Reset state to re-attempt a specific phase. Each phase is idempotent.
@{ phase = 2; status = 'running'; lastUpdated = (Get-Date).ToString('o'); error = $null } |
    ConvertTo-Json | Set-Content 'C:\ProgramData\siteops\aksee-bootstrap\state.json'
Start-ScheduledTask -TaskName SiteOpsAksEeBootstrap
```

### Full clean restart (Phase 0 or 1 failed, no cluster yet)

Use when something fundamental needs to change (different SP, different cluster name) and you want to re-deploy from scratch. Skips the cluster cleanup because no cluster exists yet at Phase 0 or 1.

```powershell
Stop-ScheduledTask       -TaskName SiteOpsAksEeBootstrap -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName SiteOpsAksEeBootstrap -Confirm:$false -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force 'C:\ProgramData\siteops\aksee-bootstrap\' -ErrorAction SilentlyContinue
Remove-LocalUser -Name siteops-bootstrap -ErrorAction SilentlyContinue

# Then re-run `siteops deploy ...`
```

### Wipe and redo from scratch (Phase 2 or 3 failed, cluster exists)

```powershell
# Stop and remove the partial cluster (only valid once AKS EE is installed)
Import-Module AksEdge -ErrorAction SilentlyContinue
Stop-AksEdgeDeployment   -Confirm:$false -ErrorAction SilentlyContinue
Remove-AksEdgeDeployment -Confirm:$false -ErrorAction SilentlyContinue

# Then run the Full clean restart block above to wipe task + working dir + user.
```

## Phases reference

| Phase | Action | May reboot? |
|---|---|---|
| 0  | Pre-flight: admin, OS, memory, disk, nested virt | No |
| 1  | MSI install + `Install-AksEdgeHostFeatures` | Yes (Hyper-V enable) |
| 2  | Substitute the Arc block in the rendered cluster config from runtime parameters, then create the single-node K3s cluster (AKS Edge Essentials Arc-connects the cluster as part of this step) | No |
| 3  | Install Azure CLI if missing, authenticate (managed identity by default, service principal as fallback), enable `cluster-connect` and `custom-locations`, and (when `enableWorkloadIdentity` is requested) wire the OIDC issuer through the K3s apiserver | No |
| 99 | Cleanup (unregister scheduled task, remove bootstrap user, remove rendered config) | No |

Each phase is idempotent so a worker re-run from any state is safe. Phase 1 writes the next phase to `state.json` BEFORE calling `Install-AksEdgeHostFeatures` so the at-startup scheduled-task trigger resumes at Phase 2 after the reboot.

Phase 3 layers AIO-specific features on top of the basic Arc-connected cluster. The reason for layering instead of doing everything in Phase 2: the inner `aksedge-config.json` schema does not recognize OIDC issuer, workload identity, or custom-locations fields. Phase 3 handles them explicitly through `az connectedk8s` commands.

## Optional flags

| Flag | Default | Effect |
|---|---|---|
| `enableWorkloadIdentity` | false | When true, Phase 3 enables OIDC issuer + workload identity on the Arc connection and patches the K3s apiserver `service-account-issuer`. Required when downstream AIO components use workload-identity-backed secret sync. |

This flag is not exposed via the Bicep parameter surface. Advanced operators enable it by invoking the launcher directly with `-EnableWorkloadIdentity` (see [Run directly](#run-directly-advanced) below).

## Secret handling

- **In transit:** the operator passes the SP secret as a manifest parameter, ultimately delivered to the launcher as a `protectedParameter` on the Arc Run Command resource. Azure encrypts the value in transit and excludes it from instance-view output. Source from a CI Key Vault binding in production.
- **At rest:** the launcher encrypts the SP password via Windows DPAPI (LocalMachine scope) before writing to `config.json`. The worker decrypts on read. Off-box exfiltration of `config.json` cannot decrypt because the DPAPI key is bound to the machine.
- **ACLs:** `C:\ProgramData\siteops\aksee-bootstrap\` has inherited ACLs removed and re-granted to Administrators + SYSTEM only.
- **Local admin user password:** generated on-box, never transmitted, written only to the Scheduled Task registration via `Register-ScheduledTask -User -Password`. The password does not persist in `config.json` or any other file the worker reads.
- **Phase 2 rendered config:** the worker writes a rendered AKS Edge config to disk that carries the plaintext SP secret while the install cmdlet reads it. A `try/finally` wraps the cmdlet invocation and always deletes the rendered file after the cmdlet returns. Phase 99 zeros the SP blob in `config.json` after the bootstrap succeeds.

## Run directly (advanced)

The scalekit path delivers the launcher via Bicep + Arc Run Command. For debugging or one-off use without scalekit, the full launcher script can run directly on the VM. See `scripts/README.md` for the dev workflow.

## Known limitations

- The Bicep `spAppId` and `spPassword` are required (no managed-identity-only path for cluster creation). AKS Edge Essentials' install cmdlet hard-requires SP credentials and there is no flag to skip its own Arc-connect. Tracked upstream.
- The bootstrap registers a Scheduled Task that runs as a dedicated local admin user the launcher creates. Hardened environments that disallow new local users would need a `LocalAdminUser` override pointing at a pre-existing account.
- The Run Command resource returns `executionState=Succeeded` the moment the launcher returns `REGISTERED`, NOT when the worker reaches `phase=99 status=succeeded`. Pair this manifest with a status-gate Bicep step to gate downstream steps on actual bootstrap completion.
