metadata description = 'Creates an Azure Device Registry namespace for use with Azure IoT Operations.'

/*****************************************************************************/
/*                          Deployment Parameters                            */
/*****************************************************************************/

@description('Name of the ADR namespace to create.')
param adrNamespaceName string

@description('Location for the namespace. Defaults to resource group location.')
param location string = resourceGroup().location

@description('Tags to apply to resources')
param tags object = {}

/*****************************************************************************/
/*                          ADR Namespace Resource                           */
/*****************************************************************************/

resource adrNamespace 'Microsoft.DeviceRegistry/namespaces@2025-10-01' = {
  name: adrNamespaceName
  location: location
  properties: {}
  tags: tags
}

/*****************************************************************************/
/*                          Deployment Outputs                               */
/*****************************************************************************/

output adrNamespace object = {
  id: adrNamespace.id
  name: adrNamespace.name
}
