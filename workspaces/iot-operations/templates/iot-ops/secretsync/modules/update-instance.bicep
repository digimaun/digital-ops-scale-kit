// update-instance.bicep
// -------------------------------------------------------------------------------------
// Module: IoT Operations Instance Update
//
// Updates an existing IoT Operations instance to set the defaultSecretProviderClassRef.
// Uses a module boundary to convert runtime values from the parent template into
// compile-time parameters, satisfying Bicep's requirement that location and
// extendedLocation be calculable at deployment start.
//
// WARNING: This module re-declares the full instance via PUT. All writable properties
// for the pinned API version (2025-10-01) must be forwarded from the parent template
// to prevent data loss. ARM API versions are immutable — the property set is fixed
// for this version.
// -------------------------------------------------------------------------------------

@description('IoT Operations instance name.')
param instanceName string

@description('Instance location (from existing instance).')
param instanceLocation string

@description('Extended location resource ID — the custom location ID.')
param extendedLocationName string

@description('Instance tags.')
param instanceTags object = {}

@description('Identity type (None, UserAssigned, SystemAssigned, SystemAssigned,UserAssigned).')
param identityType string = 'None'

@description('User-assigned managed identities map (resource ID to empty object).')
param userAssignedIdentities object = {}

@description('Schema registry resource ID (required by the IoT Operations instance).')
param schemaRegistryResourceId string

@description('ADR namespace resource ID.')
param adrNamespaceResourceId string = ''

@description('Instance features map (component mode/settings). Forwarded to prevent data loss.')
param features object = {}

@description('Instance description.')
param instanceDescription string = ''

@description('Secret Provider Class resource ID to set as the default.')
param spcResourceId string

resource instance 'Microsoft.IoTOperations/instances@2025-10-01' = {
  name: instanceName
  location: instanceLocation
  extendedLocation: {
    name: extendedLocationName
    type: 'CustomLocation'
  }
  tags: instanceTags
  identity: identityType == 'None'
    ? {
        type: 'None'
      }
    : {
        type: identityType
        userAssignedIdentities: userAssignedIdentities
      }
  properties: {
    schemaRegistryRef: {
      resourceId: schemaRegistryResourceId
    }
    adrNamespaceRef: !empty(adrNamespaceResourceId)
      ? {
          resourceId: adrNamespaceResourceId
        }
      : null
    features: features
    description: instanceDescription
    defaultSecretProviderClassRef: {
      resourceId: spcResourceId
    }
  }
}

output instanceResourceId string = instance.id
