# Generated minified launcher. Edit Build-Launcher.ps1 sources, not this file.
[CmdletBinding()]
param(
[Parameter(Mandatory)] [string]$ClusterName,
[Parameter(Mandatory)] [string]$ResourceGroup,
[Parameter(Mandatory)] [string]$Subscription,
[Parameter(Mandatory)] [string]$Location,
[Parameter(Mandatory)] [string]$TenantId,
[Parameter(Mandatory)] [string]$CustomLocationsOid,
[string]$SpAppId = '',
[string]$SpPassword = '',
[Parameter(Mandatory)] [string]$AksEdgeMsiUrl,
[string]$ConfigDir         = 'C:\ProgramData\siteops\aksee-bootstrap',
[string]$ScheduledTaskName = 'SiteOpsAksEeBootstrap',
[string]$LocalAdminUser    = 'siteops-bootstrap',
[switch]$EnableWorkloadIdentity,
[switch]$Force
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ConfirmPreference = 'None'
$ProgressPreference = 'SilentlyContinue'
if ($PSVersionTable.PSEdition -ne 'Desktop') {
throw "Install-AksEeBootstrap.ps1 requires Windows PowerShell 5.1 (Desktop). Detected: $($PSVersionTable.PSEdition) $($PSVersionTable.PSVersion). Re-run with 'powershell.exe -File Install-AksEeBootstrap.ps1 ...' instead of pwsh."
}
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
$EmbeddedWorker = @'
[CmdletBinding()]
param(
[string]$ConfigDir = 'C:\ProgramData\siteops\aksee-bootstrap'
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ConfirmPreference = 'None'
$ProgressPreference = 'SilentlyContinue'
$script:StatePath    = Join-Path $ConfigDir 'state.json'
$script:ConfigPath   = Join-Path $ConfigDir 'config.json'
$script:TemplatePath = Join-Path $ConfigDir 'aksedge-config.template.json'
function Write-Log {
param([string]$Message)
$ts = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
Write-Host "[$ts] $Message"
}
function Get-State {
if (-not (Test-Path $script:StatePath)) {
throw "State file not found at $script:StatePath. The launcher writes the initial state file. For local testing, see README.md."
}
return Get-Content -Raw -Path $script:StatePath | ConvertFrom-Json
}
function Set-State {
param(
[Parameter(Mandatory)] [int]$Phase,
[Parameter(Mandatory)] [ValidateSet('running', 'pending-reboot', 'succeeded', 'failed')] [string]$Status,
[string]$ErrorText
)
$state = [pscustomobject]@{
phase       = $Phase
status      = $Status
lastUpdated = (Get-Date).ToString('o')
error       = $ErrorText
}
$tmpPath = "$script:StatePath.tmp"
$state | ConvertTo-Json | Set-Content -Path $tmpPath -Encoding UTF8
Move-Item -Path $tmpPath -Destination $script:StatePath -Force
}
function Get-Config {
if (-not (Test-Path $script:ConfigPath)) {
throw "Config file not found at $script:ConfigPath. The launcher writes the config from caller-supplied parameters."
}
return Get-Content -Raw -Path $script:ConfigPath | ConvertFrom-Json
}
function Test-IsAdmin {
$id = [System.Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object System.Security.Principal.WindowsPrincipal($id)
return $principal.IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator)
}
function Test-AksEdgeModuleInstalled {
return $null -ne (Get-Module -ListAvailable -Name AksEdge)
}
function Test-AksEdgeDeployed {
$kubeconfig = Join-Path $env:USERPROFILE '.kube\config'
if (-not (Test-Path $kubeconfig)) { return $false }
if (-not (Get-Command kubectl -ErrorAction SilentlyContinue)) { return $false }
try {
$null = & kubectl --kubeconfig=$kubeconfig get nodes --request-timeout=5s 2>&1
return $LASTEXITCODE -eq 0
} catch {
return $false
}
}
function Resolve-SpPassword {
param($Config)
$isEncrypted = ($Config.PSObject.Properties.Name -contains 'spPasswordEncrypted') -and $Config.spPasswordEncrypted
if (-not $isEncrypted) {
return $Config.spPassword
}
Add-Type -AssemblyName System.Security
$protectedBytes = [Convert]::FromBase64String($Config.spPassword)
$plainBytes = [System.Security.Cryptography.ProtectedData]::Unprotect(
$protectedBytes,
$null,
[System.Security.Cryptography.DataProtectionScope]::LocalMachine)
return [System.Text.Encoding]::UTF8.GetString($plainBytes)
}
function Install-AzCliIfMissing {
if (Get-Command az -ErrorAction SilentlyContinue) {
Write-Log 'az CLI already present, skipping install'
return
}
$msiUrl  = 'https://aka.ms/installazurecliwindowsx64'
$msiPath = Join-Path $ConfigDir 'azurecli-installer.msi'
$log     = Join-Path $ConfigDir 'az-msiexec.log'
Write-Log "az CLI not on PATH. Downloading MSI from $msiUrl"
Invoke-WebRequest -Uri $msiUrl -OutFile $msiPath -UseBasicParsing
Write-Log "Installing az CLI MSI via msiexec /quiet, log at $log"
$proc = Start-Process msiexec.exe -Wait -PassThru -ArgumentList @(
'/i', $msiPath, '/quiet', '/norestart', '/L*V', $log
)
if ($proc.ExitCode -ne 0) {
throw "az CLI MSI install failed (exit $($proc.ExitCode)). See $log."
}
Remove-Item $msiPath -ErrorAction SilentlyContinue
$env:Path = ([Environment]::GetEnvironmentVariable('Path', 'Machine') +
[IO.Path]::PathSeparator +
[Environment]::GetEnvironmentVariable('Path', 'User'))
if (-not (Get-Command az -ErrorAction SilentlyContinue)) {
throw "az CLI install reported success but `az` is still not on PATH. Check $log."
}
Write-Log "az CLI installed: $((& az version --output tsv --query '\"azure-cli\"' 2>$null) -join ' ')"
}
function Test-ClusterArcConnected {
param([string]$ClusterName, [string]$ResourceGroup)
try {
$json = & az connectedk8s show --name $ClusterName --resource-group $ResourceGroup --output json 2>$null
if ($LASTEXITCODE -ne 0) { return $false }
$cluster = $json | ConvertFrom-Json
return $cluster.connectivityStatus -eq 'Connected'
} catch {
return $false
}
}
function Wait-ArcClusterReady {
param(
[string]$ClusterName,
[string]$ResourceGroup,
[int]$RetrySeconds = 10,
[int]$MaxRetries   = 60,
[switch]$WaitForIssuerUrl
)
for ($i = 0; $i -lt $MaxRetries; $i++) {
$json = & az connectedk8s show --name $ClusterName --resource-group $ResourceGroup --output json 2>$null
if ($LASTEXITCODE -eq 0) {
$cluster = $json | ConvertFrom-Json
$obj = if ($cluster.PSObject.Properties.Name -contains 'properties') { $cluster.properties } else { $cluster }
$connStatus = $obj.connectivityStatus
$issuerUrl = $null
if ($obj.PSObject.Properties.Name -contains 'oidcIssuerProfile' -and $null -ne $obj.oidcIssuerProfile) {
$issuerUrl = $obj.oidcIssuerProfile.issuerUrl
}
Write-Log "Arc cluster status: connectivity=$connStatus issuerUrl=$(if ($issuerUrl) { 'present' } else { '(none)' })"
if ($connStatus -eq 'Connected') {
if (-not $WaitForIssuerUrl) {
return
}
if (-not [string]::IsNullOrEmpty($issuerUrl)) {
return $issuerUrl
}
}
}
Start-Sleep -Seconds $RetrySeconds
}
throw "Cluster $ClusterName did not reach $(if ($WaitForIssuerUrl) { 'connected + OIDC-issuer-provisioned' } else { 'connected' }) status within $($MaxRetries * $RetrySeconds)s"
}
function Patch-K3sApiServer {
param([string]$IssuerUrl)
Import-Module AksEdge
Write-Log "Patching K3s apiserver service-account-issuer to $IssuerUrl"
Invoke-AksEdgeNodeCommand -command "sudo cat /var/.eflow/config/k3s/k3s-config.yml | tee /home/aksedge-user/k3s-config.yml | tee /home/aksedge-user/k3s-config.yml.working > /dev/null" | Out-Null
Invoke-AksEdgeNodeCommand -command "sudo sed -i 's|service-account-issuer.*|service-account-issuer=$IssuerUrl|' /home/aksedge-user/k3s-config.yml" | Out-Null
Invoke-AksEdgeNodeCommand -command "sudo cp /home/aksedge-user/k3s-config.yml /var/.eflow/config/k3s/k3s-config.yml" | Out-Null
$verify = Invoke-AksEdgeNodeCommand -command 'sudo grep service-account-issuer /var/.eflow/config/k3s/k3s-config.yml'
if ($verify -notmatch [regex]::Escape($IssuerUrl)) {
throw "Patch-K3sApiServer verification failed. Expected `'service-account-issuer=$IssuerUrl`' in k3s-config.yml but observed: $verify"
}
Write-Log 'Verified service-account-issuer is patched in k3s-config.yml'
Write-Log 'Restarting k3s.service to load the patched config'
Invoke-AksEdgeNodeCommand -command 'sudo systemctl restart k3s.service' | Out-Null
}
function Wait-K3sApiServerReady {
param([int]$MaxRetries = 120)
for ($i = 0; $i -lt $MaxRetries; $i++) {
$ret = & kubectl get --raw='/readyz' --request-timeout=2s 2>$null
if ($ret -eq 'ok') {
Write-Log 'K3s apiserver ready'
return
}
Start-Sleep -Seconds 1
}
throw 'K3s apiserver did not become ready within 120 seconds'
}
function Invoke-Phase0 {
param($config)
Write-Log 'Phase 0: pre-flight verification'
if (-not (Test-IsAdmin)) {
throw 'Worker must run as Administrator. Install-AksEdgeHostFeatures and New-AksEdgeDeployment both require it.'
}
$os = Get-CimInstance Win32_OperatingSystem
Write-Log "OS: $($os.Caption) ($($os.Version))"
$caption = $os.Caption.ToLower()
$supported = $false
if ($caption -like '*server 2022*' -or $caption -like '*server 2025*') {
$supported = $true
} elseif ($caption -match 'windows 1[01]') {
if ($caption -like '*pro*' -or $caption -like '*enterprise*' -or $caption -like '*iot*') {
$supported = $true
}
}
if (-not $supported) {
throw "Unsupported OS for AKS Edge Essentials: $($os.Caption). Needs Windows 10/11 Pro/Enterprise/IoT or Windows Server 2022/2025."
}
$memGB = [math]::Round($os.TotalVisibleMemorySize / 1MB, 1)
Write-Log "Total memory: $memGB GB"
if ($memGB -lt 12) {
throw "Host has $memGB GB memory. AKS EE Linux guest requests 10 GB and the Windows host needs ~2 GB headroom (12 GB minimum). AIO additionally recommends 16 GB total."
}
$sysDrive = $env:SystemDrive.TrimEnd(':')
$drive = Get-PSDrive -Name $sysDrive
$freeGB = [math]::Round($drive.Free / 1GB, 1)
Write-Log "Free space on ${sysDrive}: $freeGB GB"
if ($freeGB -lt 30) {
throw "Only $freeGB GB free on $($env:SystemDrive). AKS EE VM + container images need at least 30 GB."
}
$cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
Write-Log "VirtualizationFirmwareEnabled: $($cpu.VirtualizationFirmwareEnabled)"
if ($cpu.VirtualizationFirmwareEnabled -ne $true) {
Write-Log 'WARNING: nested virtualization may not be available. Phase 1 will fail loudly if it is.'
}
Write-Log 'Bootstrapping NuGet PSPackageProvider for AKS EE Arc-connect path'
try {
$nuget = Get-PackageProvider -Name NuGet -ListAvailable -ErrorAction SilentlyContinue |
Sort-Object Version -Descending | Select-Object -First 1
if (-not $nuget -or $nuget.Version -lt [version]'2.8.5.201') {
Install-PackageProvider -Name NuGet -MinimumVersion 2.8.5.201 -Force -Scope AllUsers -ErrorAction Stop | Out-Null
Write-Log 'NuGet PSPackageProvider installed'
} else {
Write-Log "NuGet PSPackageProvider already present (version $($nuget.Version))"
}
Set-PSRepository -Name PSGallery -InstallationPolicy Trusted -ErrorAction SilentlyContinue
} catch {
throw "Failed to bootstrap NuGet PSPackageProvider: $_. New-AksEdgeDeployment Arc-connect path will fail in Phase 2 without it."
}
Set-State -Phase 1 -Status 'running'
Write-Log 'Phase 0: complete'
}
function Invoke-Phase1 {
param($config)
Write-Log 'Phase 1: install AKS EE MSI and enable Hyper-V'
if (Test-AksEdgeModuleInstalled) {
Write-Log 'AKS Edge module already installed, skipping MSI install'
} else {
$msiPath = Join-Path $ConfigDir 'aksedge-installer.msi'
Write-Log "Downloading MSI from $($config.aksEdgeMsiUrl)"
Invoke-WebRequest -Uri $config.aksEdgeMsiUrl -OutFile $msiPath -UseBasicParsing
$fileInfo = Get-Item $msiPath
if ($fileInfo.Length -lt 50MB) {
throw "Downloaded MSI at $msiPath is only $($fileInfo.Length) bytes (expected > 50MB). The URL '$($config.aksEdgeMsiUrl)' likely returned an error page rather than the installer. Verify the URL serves application/octet-stream and a binary > 50MB."
}
$header = [byte[]]::new(8)
$fs = [System.IO.File]::OpenRead($msiPath)
try { $null = $fs.Read($header, 0, 8) } finally { $fs.Close() }
if ($header[0] -ne 0xD0 -or $header[1] -ne 0xCF -or $header[2] -ne 0x11 -or $header[3] -ne 0xE0) {
$magic = '{0:X2} {1:X2} {2:X2} {3:X2}' -f $header[0],$header[1],$header[2],$header[3]
throw "Downloaded file at $msiPath is not a valid MSI (magic bytes '$magic', expected 'D0 CF 11 E0'). The URL '$($config.aksEdgeMsiUrl)' likely returned an error page rather than the installer."
}
$msiLog = Join-Path $ConfigDir 'msiexec.log'
Write-Log "Installing MSI via msiexec /quiet /norestart, log at $msiLog"
$proc = Start-Process msiexec.exe -Wait -PassThru -ArgumentList @(
'/i', $msiPath, '/quiet', '/norestart', '/L*V', $msiLog
)
if ($proc.ExitCode -ne 0) {
throw "msiexec exited with code $($proc.ExitCode). See $msiLog for the install log."
}
Remove-Item $msiPath -ErrorAction SilentlyContinue
Write-Log 'MSI install complete'
}
$env:PSModulePath = ([Environment]::GetEnvironmentVariable('PSModulePath', 'Machine') +
[IO.Path]::PathSeparator +
[Environment]::GetEnvironmentVariable('PSModulePath', 'User'))
$env:Path = ([Environment]::GetEnvironmentVariable('Path', 'Machine') +
[IO.Path]::PathSeparator +
[Environment]::GetEnvironmentVariable('Path', 'User'))
Import-Module AksEdge
Set-State -Phase 2 -Status 'pending-reboot'
Write-Log 'Calling Install-AksEdgeHostFeatures -Force'
$result = Install-AksEdgeHostFeatures -Force
Write-Log "Install-AksEdgeHostFeatures returned: $result"
$lastResult = @($result) | Select-Object -Last 1
if ($lastResult -is [bool] -and -not $lastResult) {
throw "Install-AksEdgeHostFeatures returned `$false. See AksEdge event logs and recent entries under C:\ProgramData\AksEdge for the host-feature install failure."
}
$hvFeature = Get-WindowsOptionalFeature -Online -FeatureName Microsoft-Hyper-V-All -ErrorAction SilentlyContinue
$featureRestartNeeded = $false
if ($hvFeature -and $null -ne $hvFeature.RestartNeeded) {
$featureRestartNeeded = [bool]$hvFeature.RestartNeeded
}
$cbsPending = Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending'
if ($featureRestartNeeded -or $cbsPending) {
Write-Log "Reboot pending (Hyper-V RestartNeeded=$featureRestartNeeded, CBS RebootPending=$cbsPending). Restarting now."
Restart-Computer -Force
Start-Sleep -Seconds 60
exit 0
}
Set-State -Phase 2 -Status 'running'
Write-Log 'Phase 1: complete (no reboot needed)'
}
function Invoke-Phase2 {
param($config)
Write-Log 'Phase 2: deploy single-node K3s cluster'
if (Test-AksEdgeDeployed) {
Write-Log 'AKS EE deployment already present, skipping'
Set-State -Phase 3 -Status 'running'
return
}
if (-not (Test-Path $script:TemplatePath)) {
throw "Template not found at $script:TemplatePath. The launcher copies this alongside worker.ps1. For local testing, see README.md."
}
Import-Module AksEdge
$hasAppId    = ($config.PSObject.Properties.Name -contains 'spAppId')    -and $config.spAppId
$hasPassword = ($config.PSObject.Properties.Name -contains 'spPassword') -and $config.spPassword
if (-not ($hasAppId -and $hasPassword)) {
throw "AKS Edge Essentials requires a service principal to create the cluster. Re-run the launcher with -SpAppId and -SpPassword."
}
$renderedPath = Join-Path $ConfigDir 'aksedge-config.json'
try {
Write-Log "Rendering AKS EE config from $script:TemplatePath"
$cfg = Get-Content -Raw -Path $script:TemplatePath | ConvertFrom-Json
$cfg.Arc.ClusterName       = $config.clusterName
$cfg.Arc.Location          = $config.location
$cfg.Arc.ResourceGroupName = $config.resourceGroup
$cfg.Arc.SubscriptionId    = $config.subscription
$cfg.Arc.TenantId          = $config.tenantId
$cfg.Arc.ClientId          = $config.spAppId
$cfg.Arc.ClientSecret      = Resolve-SpPassword -Config $config
$cfg | ConvertTo-Json -Depth 6 | Set-Content -Path $renderedPath -Encoding UTF8
$cfg.Arc.ClientSecret = $null
$cfg = $null
Write-Log "Calling New-AksEdgeDeployment in child PowerShell (this typically takes 10-15 minutes)"
$childScript = "Import-Module AksEdge; New-AksEdgeDeployment -JsonConfigFilePath '$renderedPath' -Confirm:`$false -Force; exit `$LASTEXITCODE"
$bytes = [System.Text.Encoding]::Unicode.GetBytes($childScript)
$encoded = [Convert]::ToBase64String($bytes)
$psExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$childLog    = Join-Path $ConfigDir ("aksee-deploy-{0}.log" -f (Get-Date -Format 'yyyyMMdd-HHmmss'))
$childErrLog = "$childLog.err"
Write-Log "Child stdout: $childLog"
Write-Log "Child stderr: $childErrLog"
$proc = Start-Process -FilePath $psExe `
-ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-EncodedCommand', $encoded) `
-Wait -PassThru -NoNewWindow `
-RedirectStandardOutput $childLog -RedirectStandardError $childErrLog
Write-Log "New-AksEdgeDeployment child exited with code $($proc.ExitCode)"
if ($proc.ExitCode -ne 0) {
$tailOut = ''
$tailErr = ''
if (Test-Path $childLog)    { $tailOut = (Get-Content $childLog    -Tail 30 -ErrorAction SilentlyContinue) -join "`n" }
if (Test-Path $childErrLog) { $tailErr = (Get-Content $childErrLog -Tail 30 -ErrorAction SilentlyContinue) -join "`n" }
throw "New-AksEdgeDeployment exited with code $($proc.ExitCode).`nstdout tail:`n$tailOut`nstderr tail:`n$tailErr`nFull logs at $childLog and $childErrLog."
}
$kubeconfig = Join-Path $env:USERPROFILE '.kube\config'
if (-not (Test-Path $kubeconfig)) {
throw "Kubeconfig not written at $kubeconfig after New-AksEdgeDeployment. The cluster did not come up."
}
$sharedKubeconfig = Join-Path $ConfigDir 'kubeconfig'
Copy-Item -Path $kubeconfig -Destination $sharedKubeconfig -Force
Write-Log "Copied kubeconfig to $sharedKubeconfig for operator use"
Set-State -Phase 3 -Status 'running'
Write-Log 'Phase 2: complete (cluster up)'
} finally {
if (Test-Path $renderedPath) {
Write-Log "Removing rendered config at $renderedPath"
Remove-Item -Path $renderedPath -Force -ErrorAction SilentlyContinue
}
}
}
function Invoke-Phase3 {
param($config)
Write-Log 'Phase 3: Arc-connect cluster and enable AIO prereqs'
$cluster = $config.clusterName
$rg      = $config.resourceGroup
$sub     = $config.subscription
$loc     = $config.location
$tenant  = $config.tenantId
$oid     = $config.customLocationsOid
$appId   = $config.spAppId
$enableWi = ($config.PSObject.Properties.Name -contains 'enableWorkloadIdentity') -and $config.enableWorkloadIdentity
Write-Log "Workload identity + OIDC issuer requested: $enableWi"
Install-AzCliIfMissing
$env:KUBECTL_CLIENT_PATH = "$env:ProgramFiles\AksEdge\kubectl\kubectl.exe"
$sharedKubeconfig = Join-Path $ConfigDir 'kubeconfig'
if (Test-Path $sharedKubeconfig) {
$env:KUBECONFIG = $sharedKubeconfig
Write-Log "Using shared kubeconfig at $sharedKubeconfig"
} else {
Write-Log "Shared kubeconfig not found at $sharedKubeconfig. az/kubectl will fall back to `$env:USERPROFILE\.kube\config (typical when Phase 2 wrote it as the current user)."
}
Write-Log 'Adding az extension: connectedk8s'
& az extension add --name connectedk8s --upgrade --only-show-errors | Out-Null
$useMi = -not ($config.PSObject.Properties.Name -contains 'spAppId' -and $config.spAppId) `
-or -not ($config.PSObject.Properties.Name -contains 'spPassword' -and $config.spPassword)
if ($useMi) {
Write-Log 'Authenticating with Arc machine managed identity (az login --identity)'
$loginOut = & az login --identity --only-show-errors 2>&1
if ($LASTEXITCODE -ne 0) {
throw "az login --identity failed: $loginOut. Ensure the Arc machine identity has Contributor (or Kubernetes Cluster - Azure Arc Onboarding) on the resource group."
}
} else {
Write-Log "Authenticating as service principal $appId"
$spSecret = Resolve-SpPassword -Config $config
$loginOut = & az login --service-principal --username $appId --password $spSecret --tenant $tenant --only-show-errors 2>&1
if ($LASTEXITCODE -ne 0) {
throw "az login --service-principal failed for ${appId}: $loginOut"
}
}
$accountSetOut = & az account set --subscription $sub 2>&1
if ($LASTEXITCODE -ne 0) {
throw "az account set --subscription $sub failed: $accountSetOut. The authenticated principal (managed identity or service principal) likely lacks access to subscription $sub."
}
if (Test-ClusterArcConnected -ClusterName $cluster -ResourceGroup $rg) {
Write-Log "Cluster $cluster is already Arc-connected, skipping connect"
} else {
$aksEdgeVersion = (Get-Module -Name AksEdge).Version.ToString()
$tags = @('SKU=AKSEdgeEssentials', "AKSEEVersion=$aksEdgeVersion", 'ManagedBy=siteops-bootstrap')
$connectArgs = @(
'-g', $rg,
'-n', $cluster,
'-l', $loc,
'--subscription', $sub,
'--tags'
) + $tags + @(
'--disable-auto-upgrade',
'--distribution', 'aks_edge_k3s',
'--only-show-errors'
)
if ($enableWi) {
$connectArgs += @('--enable-oidc-issuer', '--enable-workload-identity')
}
Write-Log "Running az connectedk8s connect $cluster (5-10 minutes)"
$connectOut = & az connectedk8s connect @connectArgs 2>&1
if ($LASTEXITCODE -ne 0) {
throw "az connectedk8s connect failed for ${cluster}: $connectOut"
}
}
if ($enableWi) {
Write-Log 'Waiting for Arc to provision the OIDC issuer URL'
$issuerUrl = Wait-ArcClusterReady -ClusterName $cluster -ResourceGroup $rg -WaitForIssuerUrl
Write-Log "OIDC issuer URL: $issuerUrl"
Patch-K3sApiServer -IssuerUrl $issuerUrl
Wait-K3sApiServerReady
Write-Log 'Restarting Arc agents to pick up the new OIDC issuer'
& kubectl -n azure-arc rollout restart deployment | Out-Null
if ($LASTEXITCODE -ne 0) {
Write-Log 'WARNING: kubectl rollout restart returned non-zero. Check azure-arc deployments manually.'
}
Write-Log 'Waiting for azure-arc rollout to complete'
& kubectl -n azure-arc rollout status deployment --timeout=300s | Out-Null
if ($LASTEXITCODE -ne 0) {
Write-Log 'WARNING: kubectl rollout status returned non-zero. Some azure-arc deployments did not roll out within 300s. Subsequent enable-features may race the rollout.'
}
} else {
Write-Log 'Workload identity not requested. Verifying basic Arc connection only.'
Wait-ArcClusterReady -ClusterName $cluster -ResourceGroup $rg
}
Write-Log "Enabling custom-locations feature (OID $oid)"
$featuresOut = & az connectedk8s enable-features `
--name $cluster `
--resource-group $rg `
--features cluster-connect custom-locations `
--custom-locations-oid $oid `
--only-show-errors 2>&1
if ($LASTEXITCODE -ne 0) {
throw "az connectedk8s enable-features failed for ${cluster}: $featuresOut"
}
Set-State -Phase 99 -Status 'running'
Write-Log "Phase 3: complete (cluster Arc-connected, custom-locations enabled$(if ($enableWi) { ', OIDC issuer wired' }))"
}
function Invoke-Phase99 {
param($config)
Write-Log 'Phase 99: cleanup'
$taskName = 'SiteOpsAksEeBootstrap'
if ($config.PSObject.Properties.Name -contains 'scheduledTaskName') {
$taskName = $config.scheduledTaskName
}
$task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($null -ne $task) {
Write-Log "Unregistering scheduled task $taskName"
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
} else {
Write-Log "Scheduled task $taskName not found, nothing to unregister"
}
$bootstrapUser = 'siteops-bootstrap'
if ($config.PSObject.Properties.Name -contains 'localAdminUser') {
$bootstrapUser = $config.localAdminUser
}
$user = Get-LocalUser -Name $bootstrapUser -ErrorAction SilentlyContinue
$bootstrapSid = $null
if ($null -ne $user) {
$bootstrapSid = $user.SID.Value
Write-Log "Removing local user $bootstrapUser (SID $bootstrapSid)"
Remove-LocalUser -Name $bootstrapUser
} else {
Write-Log "Local user $bootstrapUser not found, nothing to remove"
}
if ($bootstrapSid) {
$profile = Get-CimInstance -ClassName Win32_UserProfile -Filter "SID='$bootstrapSid'" -ErrorAction SilentlyContinue
if ($null -ne $profile) {
try {
Remove-CimInstance -InputObject $profile -ErrorAction Stop
Write-Log "Removed user profile $($profile.LocalPath)"
} catch {
Write-Log "WARNING: failed to remove user profile $($profile.LocalPath): $_. The kubeconfig under .kube\config persists on disk. Remove manually if needed."
}
}
}
$renderedPath = Join-Path $ConfigDir 'aksedge-config.json'
if (Test-Path $renderedPath) {
Remove-Item -Path $renderedPath -Force -ErrorAction SilentlyContinue
Write-Log 'Removed leftover rendered config'
}
if (Test-Path $script:ConfigPath) {
$cfg = Get-Content -Raw -Path $script:ConfigPath | ConvertFrom-Json
if (($cfg.PSObject.Properties.Name -contains 'spPassword') -and $cfg.spPassword) {
$cfg.spPassword = ''
if ($cfg.PSObject.Properties.Name -contains 'spPasswordEncrypted') {
$cfg.spPasswordEncrypted = $false
}
$cfg | ConvertTo-Json | Set-Content -Path $script:ConfigPath -Encoding UTF8
Write-Log 'Zeroed SP password blob in config.json'
}
}
Set-State -Phase 99 -Status 'succeeded'
Write-Log 'Phase 99: complete. Bootstrap succeeded.'
}
if (-not (Test-Path $ConfigDir)) {
New-Item -ItemType Directory -Path $ConfigDir -Force | Out-Null
}
$logPath = Join-Path $ConfigDir "worker-$(Get-Date -Format 'yyyyMMdd-HHmmss').log"
Start-Transcript -Path $logPath -Append | Out-Null
try {
Write-Log "Worker started. ConfigDir=$ConfigDir Log=$logPath"
while ($true) {
$state  = Get-State
$config = Get-Config
$startPhase = $state.phase
Write-Log "Resuming at phase=$startPhase status=$($state.status)"
try {
switch ($state.phase) {
0  { Invoke-Phase0  -config $config }
1  { Invoke-Phase1  -config $config }
2  { Invoke-Phase2  -config $config }
3  { Invoke-Phase3  -config $config }
99 { Invoke-Phase99 -config $config }
default { throw "Unknown phase: $($state.phase)" }
}
} catch {
Write-Log "ERROR in phase ${startPhase}: $_"
Set-State -Phase $startPhase -Status 'failed' -ErrorText $_.ToString()
throw
}
$newState = Get-State
if ($newState.phase -eq 99 -and $newState.status -eq 'succeeded') {
Write-Log 'Bootstrap complete.'
break
}
if ($newState.status -eq 'pending-reboot') {
Write-Log 'Pending reboot. Worker exits.'
break
}
if ($newState.phase -eq $startPhase) {
Write-Log "Phase $startPhase did not advance. Stopping cascade to avoid infinite loop."
break
}
}
} finally {
Stop-Transcript | Out-Null
}
'@
$EmbeddedTemplate = @'
{"_comment":"AKS Edge Essentials single-node K3s cluster config. The bootstrap substitutes the Arc block (ClusterName, Location, ResourceGroupName, SubscriptionId, TenantId, ClientId, ClientSecret) from runtime parameters before deploying, so the nulls here are placeholders. Cluster sizing (CpuCount, MemoryInMB, DataSizeInGB, LogSizeInGB) is fixed in this file. Rebuild the launcher with Build-Launcher.ps1 after editing.","SchemaVersion":"1.16","Version":"1.0","DeploymentType":"SingleMachineCluster","Init":{"ServiceIPRangeSize":10},"Arc":{"ClusterName":null,"Location":null,"ResourceGroupName":null,"SubscriptionId":null,"TenantId":null,"ClientId":null,"ClientSecret":null},"Network":{"NetworkPlugin":"flannel","Ip4AddressPrefix":null,"InternetDisabled":false,"SkipDnsCheck":false,"Proxy":{"Http":null,"Https":null,"No":"localhost,127.0.0.0/8,192.168.0.0/16,172.17.0.0/16,10.42.0.0/16,10.43.0.0/16,10.96.0.0/12,10.244.0.0/16,.svc"}},"User":{"AcceptEula":true,"AcceptOptionalTelemetry":false},"Machines":[{"LinuxNode":{"CpuCount":4,"MemoryInMB":10240,"MemoryHugePages":{"Size":null,"Count":null},"DataSizeInGB":40,"LogSizeInGB":4,"TimeoutSeconds":300,"TpmPassthrough":false}}]}
'@
if (-not (Test-IsAdmin)) {
throw 'Install-AksEeBootstrap.ps1 must run as Administrator.'
}
Write-Log "Bootstrapping cluster $ClusterName in $ResourceGroup ($Location)"
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
if ((Test-Path $statePath) -and -not $Force) {
$inFlight = $false
$existingPhase = $null
$existingStatus = $null
try {
$existing = Get-Content -Raw -Path $statePath | ConvertFrom-Json
if (($existing.PSObject.Properties.Name -contains 'status') -and
($existing.status -in @('running', 'pending-reboot'))) {
$inFlight = $true
$existingStatus = $existing.status
if ($existing.PSObject.Properties.Name -contains 'phase') {
$existingPhase = $existing.phase
}
}
} catch {
Write-Log "WARNING: existing state.json could not be parsed. Re-initializing. ($_)"
}
if ($inFlight) {
throw "Bootstrap already in flight (state.json shows phase=$existingPhase status=$existingStatus). Pass -Force to reset state and re-register the task, or wait for the existing run to complete."
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
