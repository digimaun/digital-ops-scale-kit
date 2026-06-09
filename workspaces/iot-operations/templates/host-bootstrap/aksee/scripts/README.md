# AKS Edge Essentials bootstrap scripts

The PowerShell scripts the bootstrap delivers to the target Windows VM. The Bicep template at `../template.bicep` inlines the minified launcher via `loadTextContent`; the launcher embeds the worker and the AKS Edge config template as here-strings and registers a Scheduled Task that drives the worker through all phases.

For operator usage (configure a site, deploy via siteops, monitor, verify, troubleshoot) see [`../README.md`](../README.md). This README covers the build workflow.

## Files in this folder

| File | Role | Hand-edit? |
|---|---|---|
| `worker.ps1` | Phase-driven state machine that runs on the VM. Source. | Yes |
| `launcher-template.ps1` | Launcher source with `__EMBEDDED_*__` sentinels for the worker and the AKS Edge config template. | Yes |
| `aksedge-config.template.json` | AKS Edge Essentials cluster config template. Arc block placeholders are substituted by the worker at runtime from `config.json`. | Yes (cluster sizing, networking) |
| `Build-Launcher.ps1` | Generator. Combines the worker + AKS Edge template into the launcher and emits both full and minified variants. Parse-checks both. | No (run after editing sources) |
| `Install-AksEeBootstrap.ps1` | Generated full launcher. Operator-direct invocation form. | No (regenerated) |
| `Install-AksEeBootstrap.min.ps1` | Generated minified launcher. The Bicep `loadTextContent` references this. | No (regenerated) |
| `config.example.json` | Hand-fill example for direct-worker invocation (local testing without the launcher). | Reference only |

## Build workflow

After editing any of `worker.ps1`, `launcher-template.ps1`, or `aksedge-config.template.json`, regenerate both launcher variants:

```powershell
cd workspaces/iot-operations/templates/host-bootstrap/aksee/scripts
.\Build-Launcher.ps1
```

Output:

```
Generated Install-AksEeBootstrap.ps1 (<N> lines, parse OK)
Generated Install-AksEeBootstrap.min.ps1 (<N> lines, <N> bytes, parse OK)
```

The generator parse-checks both variants and exits non-zero on failure. The minified variant is what the Bicep delivers via Arc Run Command. The full variant is for operator-direct use on the VM. Do NOT hand-edit the generated files; they are overwritten on every build.

### Size constraints

The minified launcher must fit under the empirical `Microsoft.HybridCompute/machines/runCommands` size boundary that the Bicep delivery hits. Microsoft does not document an explicit limit; in practice the resource provider returns HCRP413 once the script body exceeds roughly 38 KB raw (JSON encoding plus ARM envelope amplify the wire-size further). The generator warns at 36 KB to give early notice. If the minified launcher approaches the boundary, options are:

1. Trim source comments and dead code.
2. Switch to `scriptUri` delivery (SAS URL to a blob) which has no documented size limit. Adds a storage dependency.

## Direct worker invocation (local testing)

Run the worker directly on a VM without the launcher and Scheduled Task. Useful for iterating on Phase 3 logic without re-deploying the launcher.

```powershell
$dir = 'C:\test\bootstrap'
New-Item -ItemType Directory -Path $dir -Force | Out-Null

# Copy the worker and the AKS Edge config template into the test dir
Copy-Item .\worker.ps1                       $dir\
Copy-Item .\aksedge-config.template.json     $dir\
Copy-Item .\config.example.json              $dir\config.json

# Edit $dir\config.json: fill in real spPassword and verify other values
notepad $dir\config.json

# Seed initial state
@{ phase = 0; status = 'running'; lastUpdated = (Get-Date).ToString('o') } |
    ConvertTo-Json | Set-Content $dir\state.json

# Run as Administrator
.\worker.ps1 -ConfigDir $dir
```

In this path, `spPassword` is plaintext (no DPAPI encryption). The worker reads it as-is because `spPasswordEncrypted` is absent from `config.json`. The launcher's DPAPI encryption is the production path.

## Phase numbers

Phase numbering is structural to the worker (state machine, reboot-survival anchor points, `state.json` field, log message prefixes, function names like `Invoke-Phase2`). For phase semantics see [`../README.md`](../README.md) under "Phases reference".

## Generation conventions

- The worker source cannot contain a here-string opener (`@'` or `@"`). The minifier strips leading whitespace from every kept line, which would corrupt here-string body indentation. `Build-Launcher.ps1` enforces this with a guard.
- The aksedge-config template is minified via `ConvertFrom-Json | ConvertTo-Json -Compress` before embedding.
- A banner at the top of each generated file marks it as do-not-edit.
