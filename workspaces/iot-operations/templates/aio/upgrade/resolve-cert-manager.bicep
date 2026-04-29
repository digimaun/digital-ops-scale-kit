// resolve-cert-manager.bicep
// -------------------------------------------------------------------------------------
// Conditionally resolves the cert-manager Arc extension by its scalekit-owned constant
// name when the cluster's trust source is SelfSigned.
//
// cert-manager is intentionally NOT part of the AIO custom location's clusterExtensionIds
// (it is cluster-wide infra outside the CL boundary), so it cannot be discovered via the
// type-iteration pattern used in resolve-extensions.bicep. Instead, its presence is
// signalled by the `trustSource` chained in from resolve-extensions (read from the AIO
// extension's configurationSettings.trustSource at install time).
//
// This module exists as a separate step (rather than inlined into resolve-extensions)
// because Bicep does not allow `if (condition)` on `existing` resource declarations to
// depend on values produced at deployment runtime by module outputs (BCP182). Splitting
// the resolution across manifest steps lets the chained trustSource act as a
// deployment-start parameter here.
//
// Output shape mirrors the per-extension snapshot shape from resolve-cluster-extension
// so update-extensions can consume aio, secretStore, and certManager uniformly.
// -------------------------------------------------------------------------------------

import {
  certManagerExtensionName
  certManagerExtensionType
} from '../../common/extension-names.bicep'

@description('Name of the Arc-connected cluster hosting the AIO instance.')
param connectedClusterName string

@description('Effective trust source for the cluster, chained from resolve-extensions.outputs.detectedTrustSource. cert-manager is resolved when this equals "SelfSigned".')
param trustSource string

@description('Optional override. Empty (default) honors the chained trustSource. Set to "SelfSigned" to force cert-manager resolution, or "CustomerManaged" to skip it. Defensive escape hatch in case the AIO RP changes configurationSettings readback semantics.')
@allowed([
  ''
  'SelfSigned'
  'CustomerManaged'
])
param trustSourceOverride string = ''

var effectiveTrustSource = !empty(trustSourceOverride) ? trustSourceOverride : trustSource
var certManagerExpected = effectiveTrustSource == 'SelfSigned'

resource cluster 'Microsoft.Kubernetes/connectedClusters@2024-07-15-preview' existing = {
  name: connectedClusterName
}

resource certManager 'Microsoft.KubernetesConfiguration/extensions@2023-05-01' existing = if (certManagerExpected) {
  scope: cluster
  name: certManagerExtensionName
}

@description('cert-manager extension snapshot. Populated when present is true; otherwise zero-valued with the canonical name and type so update-extensions can consume a uniform shape.')
output snapshot object = certManagerExpected
  ? {
      id: certManager!.id
      name: certManager!.name
      extensionType: certManager!.properties.extensionType
      version: certManager!.properties.?version ?? ''
      releaseTrain: certManager!.properties.?releaseTrain ?? ''
      configurationSettings: certManager!.properties.?configurationSettings ?? {}
      identity: certManager!.?identity ?? { type: 'None' }
    }
  : {
      id: ''
      name: certManagerExtensionName
      extensionType: certManagerExtensionType
      version: ''
      releaseTrain: ''
      configurationSettings: {}
      identity: { type: 'None' }
    }

@description('True when cert-manager is present on the cluster (effective trust source is SelfSigned). update-extensions uses this to gate the cert-manager PUT.')
output present bool = certManagerExpected

@description('Effective trust source applied (after override).')
output trustSource string = effectiveTrustSource

@description('Operator warning surfaced when no trust source could be detected and no override was provided. Empty when detection succeeded or when override forced a decision. cert-manager is silently skipped when this is non-empty unless the operator sets trustSourceOverride.')
output trustSourceWarning string = (empty(trustSource) && empty(trustSourceOverride))
  ? 'WARNING: trustSource not detected in AIO extension configurationSettings (older install or out-of-band deployment). cert-manager will NOT be upgraded by this run. If cert-manager is present, set trustSourceOverride=SelfSigned and re-run; otherwise this is the expected no-op.'
  : ''
