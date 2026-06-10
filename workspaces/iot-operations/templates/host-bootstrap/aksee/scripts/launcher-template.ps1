<#
.SYNOPSIS
Launcher for the AKS Edge Essentials + AIO bootstrap. Writes the worker
state-machine to disk, registers a Scheduled Task that drives it through
all phases (including the Hyper-V reboot), and returns once the task is
registered. Intended for either direct invocation on a Windows VM or
delivery via Azure Arc run-command.

.DESCRIPTION
The launcher is self-contained. It embeds `worker.ps1` and the AKS EE
config template as here-strings, so the entire bootstrap can be delivered
as a single `Microsoft.HybridCompute/machines/runCommands` script body.

Steps:
  1. Verify admin privileges and tighten ACLs on the config directory.
  2. Write the embedded worker and template to the config directory.
  3. Create (or refresh) a dedicated local admin user for the Scheduled
     Task. The password is generated on-box and never transmitted.
  4. Encrypt the caller-supplied service principal password via DPAPI
     (LocalMachine scope) so it does not sit in plaintext on disk.
  5. Write `config.json` and the initial `state.json` (phase=0).
  6. Register a Scheduled Task with at-startup + immediate triggers that
     runs `worker.ps1` as the local admin user.
  7. Start the task and return `REGISTERED` so the caller sees success.

The Scheduled Task survives reboots (at-startup trigger) so Phase 1's
Hyper-V enablement does not lose state.

.PARAMETER ClusterName
Name of the Arc-connected Kubernetes cluster that AKS EE will register.
Must match the `clusterName` the scalekit site overlay expects.

.PARAMETER ResourceGroup
Resource group that holds the Arc-connected server resource and will
receive the new connectedClusters resource.

.PARAMETER Subscription
Subscription ID.

.PARAMETER Location
Azure region for the connectedClusters and custom-location resources.

.PARAMETER TenantId
Azure AD tenant ID for the service principal.

.PARAMETER CustomLocationsOid
Tenant-wide object ID for the Custom Locations RP service principal.

.PARAMETER SpAppId
Service principal application ID. AKS Edge Essentials requires SP credentials
to create the cluster (Phase 2), so this is part of the standard happy path.
Leave empty only when running the bootstrap against an already-existing
cluster (Phase 2 short-circuits via deployment detection, and Phase 3 falls
through to the machine's managed identity). Must be paired with SpPassword.

.PARAMETER SpPassword
Service principal client secret. Required when SpAppId is set. Encrypted
at rest before the worker reads it back. The launcher reads this once and
discards the plaintext after encryption.

.PARAMETER AksEdgeMsiUrl
URL of the AKS Edge Essentials MSI to install. Pin a known-good version.

.PARAMETER ConfigDir
Directory holding all worker artifacts. Defaults to
`C:\ProgramData\siteops\aksee-bootstrap`. Override for local testing.

.PARAMETER ScheduledTaskName
Name of the Scheduled Task. Defaults to `SiteOpsAksEeBootstrap`. Set
explicitly only if multiple bootstraps run on the same host.

.PARAMETER LocalAdminUser
Local user the Scheduled Task runs as. Defaults to `siteops-bootstrap`.
The launcher creates the user (or resets its password) and adds it to
the local Administrators group.

.EXAMPLE
    # Standard happy path. SP creates the cluster (Phase 2); Phase 3
    # operations use the Arc machine's managed identity by default.
    .\Install-AksEeBootstrap.ps1 `
        -ClusterName        aksee-cluster1 `
        -ResourceGroup      aksee-rg `
        -Subscription       00000000-0000-0000-0000-000000000000 `
        -Location           westus3 `
        -TenantId           00000000-0000-0000-0000-000000000000 `
        -CustomLocationsOid 00000000-0000-0000-0000-000000000000 `
        -SpAppId            00000000-0000-0000-0000-000000000000 `
        -SpPassword         <secret> `
        -AksEdgeMsiUrl      https://aka.ms/aks-edge/k3s-msi

.EXAMPLE
    # Advanced: run against an already-existing cluster. Phase 2 detects
    # the existing cluster and short-circuits, so SP is not needed. Phase 3
    # uses the Arc machine's managed identity for the Arc operations.
    .\Install-AksEeBootstrap.ps1 `
        -ClusterName        aksee-cluster1 `
        -ResourceGroup      aksee-rg `
        -Subscription       00000000-0000-0000-0000-000000000000 `
        -Location           westus3 `
        -TenantId           00000000-0000-0000-0000-000000000000 `
        -CustomLocationsOid 00000000-0000-0000-0000-000000000000 `
        -AksEdgeMsiUrl      https://aka.ms/aks-edge/k3s-msi

.NOTES
Generated from `launcher-template.ps1` + `worker.ps1` +
`aksedge-config.template.json` by `Build-Launcher.ps1`. Regenerate after
editing any of those sources.
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string]$ClusterName,
    [Parameter(Mandatory)] [string]$ResourceGroup,
    [Parameter(Mandatory)] [string]$Subscription,
    [Parameter(Mandatory)] [string]$Location,
    [Parameter(Mandatory)] [string]$TenantId,
    [Parameter(Mandatory)] [string]$CustomLocationsOid,
    # SpAppId + SpPassword. AKS Edge Essentials requires SP credentials
    # to create the cluster in Phase 2, so both fields are part of the
    # standard happy path. Both can be omitted only when running against
    # an already-existing cluster (Phase 2 detects the cluster and skips
    # the create; Phase 3 falls through to the machine's managed identity).
    # The launcher enforces "both or neither" so the worker never sees a
    # half-populated config.
    [string]$SpAppId = '',
    [string]$SpPassword = '',
    [Parameter(Mandatory)] [string]$AksEdgeMsiUrl,
    [string]$ConfigDir         = 'C:\ProgramData\siteops\aksee-bootstrap',
    [string]$ScheduledTaskName = 'SiteOpsAksEeBootstrap',
    [string]$LocalAdminUser    = 'siteops-bootstrap',
    # Off by default to match the user's validated AIO baseline. Set
    # $true when downstream AIO needs workload-identity-backed secret
    # sync. Turning this on activates the riskiest Phase 3 path
    # (apiserver patch via sed + Invoke-AksEdgeNodeCommand + k3s restart).
    [switch]$EnableWorkloadIdentity,
    # Refuse to re-init when state.json shows an in-flight bootstrap.
    # Pass -Force to reset state to phase=0 and re-register the task
    # (destroys progress of any concurrent run).
    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ConfirmPreference = 'None'
$ProgressPreference = 'SilentlyContinue'

# DPAPI LocalMachine encryption (Protect-StringMachine below) uses
# .NET Framework System.Security types that are only present under
# Windows PowerShell 5.1 ("Desktop" edition). PowerShell 7+ ("Core")
# has a different surface and would fail at Add-Type. Refuse to run
# under the wrong edition rather than producing a confusing
# Add-Type failure mid-encryption.
if ($PSVersionTable.PSEdition -ne 'Desktop') {
    throw "Install-AksEeBootstrap.ps1 requires Windows PowerShell 5.1 (Desktop). Detected: $($PSVersionTable.PSEdition) $($PSVersionTable.PSVersion). Re-run with 'powershell.exe -File Install-AksEeBootstrap.ps1 ...' instead of pwsh."
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

function Write-Log {
    param([string]$Message)
    $ts = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
    Write-Host "[$ts] [launcher] $Message"
}

function Test-IsAdmin {
    $id = [System.Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object System.Security.Principal.WindowsPrincipal($id)
    return $principal.IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Protect-StringMachine {
    # Encrypts a string with DPAPI bound to the LocalMachine scope so any
    # admin on this host (including the Scheduled Task's local admin user)
    # can decrypt. Off-box exfiltration of the file cannot decrypt.
    param([string]$Plain)
    Add-Type -AssemblyName System.Security
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($Plain)
    $protected = [System.Security.Cryptography.ProtectedData]::Protect(
        $bytes,
        $null,
        [System.Security.Cryptography.DataProtectionScope]::LocalMachine)
    return [Convert]::ToBase64String($protected)
}

function New-RandomPassword {
    # Generate a strong password for the local admin user. 24 chars from
    # the printable ASCII range, biased to satisfy Windows complexity
    # rules (upper, lower, digit, symbol).
    $upper = 'ABCDEFGHJKLMNPQRSTUVWXYZ'
    $lower = 'abcdefghijkmnpqrstuvwxyz'
    $digit = '23456789'
    $symbol = '!@#$%^&*()-_=+'
    $all = ($upper + $lower + $digit + $symbol).ToCharArray()
    $required = @(
        (Get-Random -InputObject $upper.ToCharArray()),
        (Get-Random -InputObject $lower.ToCharArray()),
        (Get-Random -InputObject $digit.ToCharArray()),
        (Get-Random -InputObject $symbol.ToCharArray())
    )
    $rest = 1..20 | ForEach-Object { Get-Random -InputObject $all }
    $chars = $required + $rest | Sort-Object { Get-Random }
    return -join $chars
}

function Set-StrictAcl {
    # Lock the config dir to Administrators + SYSTEM only. Removes the
    # inherited Users-read grant from ProgramData so non-admin local users
    # cannot read the (encrypted) SP secret blob.
    #
    # icacls is a native binary, so a non-zero exit does NOT raise under
    # $ErrorActionPreference='Stop'. Check $LASTEXITCODE explicitly after
    # each call. A silent /inheritance:r failure here would leave the
    # default ProgramData ACL in place (BUILTIN\Users: ReadAndExecute +
    # Write), and the subsequent /grant would only ADD entries without
    # stripping the inherited Users grant, breaking the secret-at-rest
    # guarantee the README describes.
    param([string]$Path)
    $inheritOut = & icacls $Path /inheritance:r 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "icacls /inheritance:r failed on ${Path} with exit ${LASTEXITCODE}: $inheritOut"
    }
    $grantOut = & icacls $Path /grant 'Administrators:(OI)(CI)F' 'SYSTEM:(OI)(CI)F' 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "icacls /grant failed on ${Path} with exit ${LASTEXITCODE}: $grantOut"
    }
    Write-Log "Locked ACLs on $Path to Administrators + SYSTEM"
}

function Set-LocalAdminUser {
    # Create or refresh the local admin user the Scheduled Task runs as.
    # Returns the plaintext password (needed for Register-ScheduledTask).
    param([string]$Username, [string]$Password)
    $secure = ConvertTo-SecureString $Password -AsPlainText -Force
    $user = Get-LocalUser -Name $Username -ErrorAction SilentlyContinue
    if ($null -eq $user) {
        Write-Log "Creating local user $Username"
        New-LocalUser -Name $Username -Password $secure -AccountNeverExpires -PasswordNeverExpires -UserMayNotChangePassword | Out-Null
    } else {
        Write-Log "Resetting password on existing local user $Username"
        Set-LocalUser -Name $Username -Password $secure
    }
    $group = Get-LocalGroupMember -Group 'Administrators' -Member $Username -ErrorAction SilentlyContinue
    if ($null -eq $group) {
        Write-Log "Adding $Username to local Administrators group"
        Add-LocalGroupMember -Group 'Administrators' -Member $Username
    }
}

# ---------------------------------------------------------------------------
# Embedded payloads
# ---------------------------------------------------------------------------

# === BEGIN EMBEDDED WORKER ===
$EmbeddedWorker = @'
__EMBEDDED_WORKER_PS1__
'@
# === END EMBEDDED WORKER ===

# === BEGIN EMBEDDED TEMPLATE ===
$EmbeddedTemplate = @'
__EMBEDDED_AKSEDGE_TEMPLATE__
'@
# === END EMBEDDED TEMPLATE ===

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if (-not (Test-IsAdmin)) {
    throw 'Install-AksEeBootstrap.ps1 must run as Administrator.'
}

Write-Log "Bootstrapping cluster $ClusterName in $ResourceGroup ($Location)"

# Preflight: fail fast on unreachable or wrong-content MSI URL so we do
# not register a task that will fail Phase 1. HEAD request avoids a full
# binary download. Validates three things:
#   1. Status 200
#   2. Content-Type is NOT text/* (a wrong aka.ms link returns an HTML
#      error page with Content-Type text/html, which would otherwise
#      sail through to msiexec exit 1620).
#   3. Content-Length is > 50MB (the real AKS EE MSI is ~876MB; an HTML
#      error blob is typically < 1MB).
try {
    Write-Log "Pre-checking AKS EE MSI URL $AksEdgeMsiUrl"
    $head = Invoke-WebRequest -Uri $AksEdgeMsiUrl -Method Head -UseBasicParsing -ErrorAction Stop
    if ($head.StatusCode -ne 200) {
        throw "Unexpected status $($head.StatusCode) from MSI URL preflight."
    }
    $ct = $head.Headers['Content-Type']
    if ($ct -match '^text/') {
        throw "MSI URL returned Content-Type '$ct' (expected application/octet-stream or application/x-msi). The URL likely redirects to an error page rather than the installer."
    }
    $cl = [int64]0
    $rawLen = $head.Headers['Content-Length']
    if ($rawLen) { [void][int64]::TryParse(($rawLen | Select-Object -First 1), [ref]$cl) }
    if ($cl -gt 0 -and $cl -lt 50MB) {
        throw "MSI URL returned Content-Length $cl bytes (expected > 50MB). The URL likely redirects to an error page rather than the installer."
    }
} catch {
    throw "AKS EE MSI URL preflight failed for ${AksEdgeMsiUrl}: $_"
}

if (-not (Test-Path $ConfigDir)) {
    New-Item -ItemType Directory -Path $ConfigDir -Force | Out-Null
    Write-Log "Created $ConfigDir"
}
Set-StrictAcl -Path $ConfigDir

$workerPath   = Join-Path $ConfigDir 'worker.ps1'
$templatePath = Join-Path $ConfigDir 'aksedge-config.template.json'
$configPath   = Join-Path $ConfigDir 'config.json'
$statePath    = Join-Path $ConfigDir 'state.json'

# Re-init guard. Inspect any existing state.json before resetting state and
# re-registering the task. A bootstrap that is in flight must not be clobbered,
# and one that already succeeded must not be re-run (re-running would reset to
# phase 0, and Phase 99 already removed the bootstrap user, so Phase 2 would
# collide with the existing cluster and flip the state tag to failed). -Force
# overrides both, destroying any prior state for a deliberate re-bootstrap.
if ((Test-Path $statePath) -and -not $Force) {
    $inFlight = $false
    $alreadyDone = $false
    $existingPhase = $null
    $existingStatus = $null
    try {
        $existing = Get-Content -Raw -Path $statePath | ConvertFrom-Json
        if ($existing.PSObject.Properties.Name -contains 'status') {
            $existingStatus = $existing.status
            if ($existing.status -in @('running', 'pending-reboot')) {
                $inFlight = $true
            } elseif ($existing.status -eq 'succeeded') {
                $alreadyDone = $true
            }
            if ($existing.PSObject.Properties.Name -contains 'phase') {
                $existingPhase = $existing.phase
            }
        }
    } catch {
        # Unparseable state.json: warn and continue, since re-init repairs it.
        Write-Log "WARNING: existing state.json could not be parsed. Re-initializing. ($_)"
    }
    if ($inFlight) {
        throw "Bootstrap already in flight (state.json shows phase=$existingPhase status=$existingStatus). Pass -Force to reset state and re-register the task, or wait for the existing run to complete."
    }
    if ($alreadyDone) {
        # Idempotent re-apply. The cluster is already bootstrapped and the
        # state tag already reads succeeded, so the composition wait step
        # passes without any work here. Leave state, the task, and the user
        # untouched. -Force re-bootstraps from scratch.
        Write-Log "Bootstrap already completed (state.json shows phase=$existingPhase status=succeeded). Nothing to do. Pass -Force to re-bootstrap from scratch."
        Write-Output 'ALREADY-BOOTSTRAPPED'
        return
    }
}

Set-Content -Path $workerPath   -Value $EmbeddedWorker   -Encoding UTF8
Set-Content -Path $templatePath -Value $EmbeddedTemplate -Encoding UTF8
Write-Log "Wrote $workerPath and $templatePath"

$adminPassword = New-RandomPassword
Set-LocalAdminUser -Username $LocalAdminUser -Password $adminPassword

$encryptedSpPassword = ''
$useMi = -not $SpAppId -and -not $SpPassword
if ((-not $useMi) -and (-not $SpAppId -or -not $SpPassword)) {
    throw "SpAppId and SpPassword must be provided together (SP auth) or both omitted (MI auth)."
}
if (-not $useMi) {
    $encryptedSpPassword = Protect-StringMachine -Plain $SpPassword
}

$config = [pscustomobject]@{
    clusterName            = $ClusterName
    resourceGroup          = $ResourceGroup
    subscription           = $Subscription
    location               = $Location
    tenantId               = $TenantId
    customLocationsOid     = $CustomLocationsOid
    spAppId                = $SpAppId
    spPassword             = $encryptedSpPassword
    spPasswordEncrypted    = (-not $useMi)
    aksEdgeMsiUrl          = $AksEdgeMsiUrl
    scheduledTaskName      = $ScheduledTaskName
    localAdminUser         = $LocalAdminUser
    enableWorkloadIdentity = [bool]$EnableWorkloadIdentity
}
$config | ConvertTo-Json | Set-Content -Path $configPath -Encoding UTF8
$authNote = if ($useMi) { 'managed identity' } else { 'SP password encrypted via DPAPI LocalMachine' }
Write-Log "Wrote $configPath (auth=$authNote, WI=$([bool]$EnableWorkloadIdentity))"

$initialState = [pscustomobject]@{
    phase       = 0
    status      = 'running'
    lastUpdated = (Get-Date).ToString('o')
    error       = $null
}
$initialStateTmp = "$statePath.tmp"
$initialState | ConvertTo-Json | Set-Content -Path $initialStateTmp -Encoding UTF8
Move-Item -Path $initialStateTmp -Destination $statePath -Force
Write-Log "Wrote $statePath (phase=0)"

$action = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$workerPath`" -ConfigDir `"$ConfigDir`""

# at-startup trigger handles the Hyper-V reboot resume in Phase 1.
# once-trigger ~30s out kicks off the initial run without needing a reboot.
$startupTrigger = New-ScheduledTaskTrigger -AtStartup
$onceTrigger    = New-ScheduledTaskTrigger -Once -At ((Get-Date).AddSeconds(30))

$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:COMPUTERNAME\$LocalAdminUser" `
    -LogonType Password `
    -RunLevel Highest

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 4) `
    -MultipleInstances IgnoreNew

# No auto-restart on failure. Non-transient errors (wrong subscription,
# missing RBAC, etc.) would otherwise retry 3 times at 5-min intervals,
# overwriting state.json.error each time and hiding repeated identical
# failures from operators. The worker's idempotency makes manual
# re-invocation via Start-ScheduledTask safe and explicit when an
# operator decides a retry is warranted.

$task = New-ScheduledTask `
    -Action $action `
    -Trigger @($startupTrigger, $onceTrigger) `
    -Principal $principal `
    -Settings $settings

Register-ScheduledTask `
    -TaskName $ScheduledTaskName `
    -InputObject $task `
    -User "$env:COMPUTERNAME\$LocalAdminUser" `
    -Password $adminPassword `
    -Force | Out-Null
Write-Log "Registered Scheduled Task $ScheduledTaskName"

Start-ScheduledTask -TaskName $ScheduledTaskName
Write-Log "Started $ScheduledTaskName"

Write-Output 'REGISTERED'
