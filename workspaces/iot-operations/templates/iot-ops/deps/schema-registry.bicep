metadata description = 'Creates a Schema Registry with supporting storage infrastructure for Azure IoT Operations.'

/*****************************************************************************/
/*                          Deployment Parameters                            */
/*****************************************************************************/

@description('Name of the schema registry to create.')
param schemaRegistryName string

@description('Name of the storage account. If not provided, a unique name will be generated.')
param storageAccountName string = ''

@description('Name of the blob container for schema storage.')
param containerName string = 'schemas'

@description('Location for all resources. Defaults to resource group location.')
param location string = resourceGroup().location

@description('Tags to apply to resources')
param tags object = {}

/*****************************************************************************/
/*                          Storage Account                                  */
/*****************************************************************************/

var generatedStorageAccountName = !empty(storageAccountName)
  ? storageAccountName
  : take('sr${uniqueString(resourceGroup().id, schemaRegistryName)}', 24)

resource storageAccount 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: generatedStorageAccountName
  location: location
  tags: tags
  sku: {
    name: 'Standard_LRS'
  }
  kind: 'StorageV2'
  properties: {
    isHnsEnabled: true
    accessTier: 'Hot'
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storageAccount
  name: 'default'
}

resource container 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: containerName
}

/*****************************************************************************/
/*                          Schema Registry                                  */
/*****************************************************************************/

resource schemaRegistry 'Microsoft.DeviceRegistry/schemaRegistries@2024-09-01-preview' = {
  name: schemaRegistryName
  location: location
  tags: tags
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    namespace: schemaRegistryName
    // Explicitly construct URL to avoid any trailing slash issues
    storageAccountContainerUrl: 'https://${storageAccount.name}.blob.${environment().suffixes.storage}/${containerName}'
  }
  dependsOn: [
    container
  ]
}

/*****************************************************************************/
/*                          Role Assignments                                 */
/*****************************************************************************/

// Storage Blob Data Contributor role for Schema Registry MI on the container
// Role ID: ba92f5b4-2d11-453d-a403-e96b0029c9fe
resource schemaRegistryStorageRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(container.id, schemaRegistry.id, 'ba92f5b4-2d11-453d-a403-e96b0029c9fe')
  scope: container
  properties: {
    roleDefinitionId: resourceId('Microsoft.Authorization/roleDefinitions', 'ba92f5b4-2d11-453d-a403-e96b0029c9fe')
    principalId: schemaRegistry.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

/*****************************************************************************/
/*                          Deployment Outputs                               */
/*****************************************************************************/

output schemaRegistry object = {
  id: schemaRegistry.id
  name: schemaRegistry.name
  principalId: schemaRegistry.identity.principalId
}

output storageAccount object = {
  id: storageAccount.id
  name: storageAccount.name
  containerUrl: 'https://${storageAccount.name}.blob.${environment().suffixes.storage}/${containerName}'
}
