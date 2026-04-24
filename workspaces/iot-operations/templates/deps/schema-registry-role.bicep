metadata description = 'Assigns Contributor role to AIO extension on Schema Registry.'

/*****************************************************************************/
/*                          Deployment Parameters                            */
/*****************************************************************************/

@description('Name of the existing schema registry.')
param schemaRegistryName string

@description('Principal ID of the AIO extension system-assigned identity.')
param aioExtensionPrincipalId string

/*****************************************************************************/
/*                          Existing Resources                               */
/*****************************************************************************/

resource schemaRegistry 'Microsoft.DeviceRegistry/schemaRegistries@2024-09-01-preview' existing = {
  name: schemaRegistryName
}

/*****************************************************************************/
/*                          Role Assignment                                  */
/*****************************************************************************/

// Contributor role for AIO Extension MI on Schema Registry
// Role ID: b24988ac-6180-42a0-ab88-20f7382dd24c
resource aioExtensionSchemaRegistryRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(schemaRegistry.id, aioExtensionPrincipalId, 'b24988ac-6180-42a0-ab88-20f7382dd24c')
  scope: schemaRegistry
  properties: {
    roleDefinitionId: resourceId('Microsoft.Authorization/roleDefinitions', 'b24988ac-6180-42a0-ab88-20f7382dd24c')
    principalId: aioExtensionPrincipalId
    principalType: 'ServicePrincipal'
  }
}
