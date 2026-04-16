# AIO Versions

Azure IoT Operations (AIO) ships on a release cadence; each release pins specific versions of the AIO extension, cert-manager, secret store, and a matching control-plane API version. The scalekit represents every supported release as a version config file under `workspaces/iot-operations/parameters/aio-versions/` and selects one per site via `site.properties.aioVersion`.

## How version selection works

```
site.properties.aioVersion: "2603"
            │
            ▼
workspaces/iot-operations/parameters/aio-versions/2603.yaml
            │
            ▼  (siteops auto-forwards matching params to Bicep)
templates/aio/enablement.bicep       ──► cert-manager, secret store extensions
templates/aio/instance.bicep         ──► AIO extension + instance (dispatches on aioApiVersion)
templates/secretsync/enable-secretsync.bicep  ──► instance update (dispatches on aioApiVersion)
```

Each version YAML is a flat schema:

```yaml
# parameters/aio-versions/2603.yaml
aioVersion: "1.3.38"            # AIO extension version pinned in Arc
aioTrain: stable                # Extension release train
aioApiVersion: "2026-03-01"     # Microsoft.IoTOperations/instances API version
certManagerVersion: "0.10.2"
certManagerTrain: stable
secretStoreVersion: "1.3.0"
secretStoreTrain: stable
```

The `aioApiVersion` value routes the CREATE and UPDATE operations through the matching versioned module (for example `templates/aio/modules/instance-2026-03-01.bicep`). Bicep cannot parameterize API version strings, so the dispatchers use `@allowed` + conditional modules; see [Adding a new AIO version](#adding-a-new-aio-version) below.

## Pinning a site to a version

Set `properties.aioVersion` on the site (or on a parent via inheritance). The value must be the stem of a YAML under `parameters/aio-versions/`.

```yaml
# sites/munich-prod.yaml
apiVersion: siteops/v1
kind: Site
name: munich-prod
inherits: base-site.yaml

properties:
  aioVersion: "2603"    # must match parameters/aio-versions/2603.yaml
```

If not specified, the site inherits whatever `base-site.yaml` declares (`"2603"` today).

## Available versions

Every file in `workspaces/iot-operations/parameters/aio-versions/` is a shipped version. At time of writing:

| Stem | `aioApiVersion` | Notes |
|------|-----------------|-------|
| `2512` | `2025-10-01` | |
| `2602` | `2025-10-01` | |
| `2603` | `2026-03-01` | base-site default |

Source of truth for every pinned version number is the YAML itself. Cross-reference against the [IoT Operations version matrix](https://github.com/Azure/azure-iot-ops-cli-extension/wiki/IoT-Operations-versions) before shipping a new one.

## Upgrading an existing site — **not currently supported**

> ⚠️ **In-place version upgrades are not a supported path in this release.** `aio-install.yaml` is a greenfield-install manifest. Running it against a site that already has AIO deployed will overwrite runtime state on the AIO instance and its child resources. Bump `aioVersion` only when you are prepared to redeploy the site from a clean state.

**Why this isn't supported yet:** `aio-install.yaml` re-authors the AIO instance, broker, dataflow profile, and extension configuration from Bicep source on every run. Any property set out-of-band (portal, `az iot ops`, `kubectl`) after install will be reset. Without a dedicated upgrade manifest that explicitly preserves existing state, re-running install is unsafe for anything beyond a reinstall.

**Supported today:**
- **Pin a new site to a specific version** at install time (see [Pinning a site to a version](#pinning-a-site-to-a-version)).
- **Redeploy from scratch** — tear down the AIO instance and supporting resources, then run `aio-install.yaml` with the new `aioVersion`.

A dedicated upgrade manifest is planned as a follow-up.

## Adding a new AIO version

1. **Ship the version YAML.** Create `parameters/aio-versions/<stem>.yaml` with all seven fields (`aioVersion`, `aioTrain`, `aioApiVersion`, `certManagerVersion`, `certManagerTrain`, `secretStoreVersion`, `secretStoreTrain`).
2. **If `aioApiVersion` is new**, extend the dispatch in both Bicep routers:
   - `templates/aio/instance.bicep` — add to `@allowed` on `param aioApiVersion`, add a new conditional `module instance_<YYYY>` block, push the previously-newest version from `else` into an explicit equality, make the new version the `else`.
   - `templates/aio/modules/update-instance.bicep` — same pattern; the file header has a checklist.
   - Add `templates/aio/modules/instance-<YYYY-MM-DD>.bicep` and `update-instance-<YYYY-MM-DD>.bicep`. Start by copying the previous API version's modules verbatim and change only the API version strings; diverge per-module only when the schema actually changes.
3. **If `aioApiVersion` is unchanged**, no Bicep changes are needed — siteops forwards the new extension versions via parameter auto-filtering.
4. **Run the workspace suite**: `pytest tests/workspace/ -q`. The relevant checks are:
   - `test_version_config_api_versions_are_allowed_in_bicep` — the new `aioApiVersion` must be in both dispatchers' `@allowed` lists.
   - `test_all_sites_aio_versions_have_config_files` — no site references a missing YAML stem.
   - `TestUpdateInstanceDispatch` — every param of the update-instance router is forwarded by every caller.
5. **Test live**: dispatch the E2E workflow including the new version in `aio-versions`:
   ```
   gh workflow run e2e-test.yaml -f aio-versions=<existing>,<new>
   ```
   The matrix runs each version in its own fresh RG + Arc cluster, and `test_aio_extension_version_matches_version_config` cross-checks the deployed `aioExtension.version` against the YAML.

## Validation summary

Four layers catch version misconfigurations before they reach production:

| Layer | Check | When it runs |
|-------|-------|--------------|
| Workflow prep job | Every requested `aio-versions` entry has a matching YAML | E2E dispatch (`e2e-test.yaml`) |
| Workspace unit tests | `@allowed` membership, all-sites coverage, base-site coverage | Every CI run |
| Workspace unit tests | `TestUpdateInstanceDispatch` — caller-vs-router param parity | Every CI run |
| Live integration | Deployed `aioExtension.version` equals YAML's `aioVersion` | E2E matrix (per cell) |

## See also

- [Site configuration](site-configuration.md) — the `aioVersion` field lives in `properties:`; inheritance and overlays apply normally.
- [Parameter resolution](parameter-resolution.md) — how version YAML values are auto-forwarded to Bicep.
- [E2E testing](e2e-testing.md) — how to dispatch a matrix over multiple versions.
- `templates/aio/instance.bicep` and `templates/aio/modules/update-instance.bicep` — dispatcher checklists embedded at the top of each file.
