// resolve-aio.bicep
// -------------------------------------------------------------------------------------
// Read-only template: resolves an Azure IoT Operations instance and its associated
// infrastructure (custom location, connected cluster) into a complete set of outputs.
//
// This template performs no resource creation or modification. It reads existing
// resources and outputs their properties for consumption by downstream steps via
// output chaining.
//
// Resolution chain (using module boundaries for runtime → compile-time conversion):
//   1. Instance (name is a parameter → compile-time)
//   2. Custom Location (parsed from instance.extendedLocation.name via module)
//   3. Connected Cluster (parsed from CL.hostResourceId via module)
//
// Usage:
//   This template is designed as a siteops manifest step. Its outputs feed
//   downstream steps (e.g., enable-secretsync) via parameter chaining files.
//
//   Standalone:
//     az deployment group create -g <rg> -f resolve-aio.bicep \
//       -p aioInstanceName=<instance>
// -------------------------------------------------------------------------------------

// =====================================================================================
// Parameters
// =====================================================================================

@description('Name of the existing IoT Operations instance.')
param aioInstanceName string

@description('Use the self-hosted OIDC issuer URL instead of the public one.')
param useSelfHostedIssuer bool = false

// =====================================================================================
// Existing Instance
// =====================================================================================

resource instance 'Microsoft.IoTOperations/instances@2025-10-01' existing = {
  name: aioInstanceName
}

// =====================================================================================
// Chained Resolution via Modules
//   Each module boundary converts a runtime resource ID into a compile-time
//   parameter, enabling the next existing resource lookup.
// =====================================================================================

module resolvedCl 'modules/resolve-custom-location.bicep' = {
  name: 'resolve-cl-${uniqueString(aioInstanceName)}'
  params: {
    customLocationResourceId: instance.extendedLocation.name
  }
}

module resolvedCluster 'modules/resolve-cluster.bicep' = {
  name: 'resolve-cluster-${uniqueString(aioInstanceName)}'
  params: {
    connectedClusterResourceId: resolvedCl.outputs.hostResourceId
  }
}

// =====================================================================================
// Outputs — resolved infrastructure
// =====================================================================================

@description('Full ARM resource ID of the custom location.')
output customLocationId string = instance.extendedLocation.name

@description('Custom location name.')
output customLocationName string = resolvedCl.outputs.name

@description('Kubernetes namespace associated with the custom location.')
output customLocationNamespace string = resolvedCl.outputs.namespace

@description('Connected cluster name.')
output connectedClusterName string = resolvedCluster.outputs.name

@description('OIDC issuer URL for workload identity federation.')
output oidcIssuerUrl string = useSelfHostedIssuer
  ? resolvedCluster.outputs.selfHostedIssuerUrl
  : resolvedCluster.outputs.oidcIssuerUrl

// =====================================================================================
// Outputs — instance properties (for safe PUT forwarding by downstream templates)
// =====================================================================================

@description('Instance location.')
output instanceLocation string = instance.location

@description('Instance tags. Note: ARM does not expose tags on existing resource references, so this output requires the instance to have tags set. Defaults to empty object if unavailable.')
output instanceTags object = instance.?tags ?? {}

@description('Instance identity type.')
output identityType string = instance.?identity.?type ?? 'None'

@description('Instance user-assigned identities map.')
output userAssignedIdentities object = instance.?identity.?userAssignedIdentities ?? {}

@description('Schema registry resource ID.')
output schemaRegistryResourceId string = instance.properties.schemaRegistryRef.resourceId

@description('ADR namespace resource ID.')
output adrNamespaceResourceId string = instance.properties.?adrNamespaceRef.?resourceId ?? ''

@description('Instance features map.')
output features object = instance.properties.?features ?? {}

@description('Instance description.')
output instanceDescription string = instance.properties.?description ?? ''
