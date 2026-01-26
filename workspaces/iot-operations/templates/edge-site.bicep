metadata description = 'Creates an Azure Edge site resource for organizing edge deployments.'

/*****************************************************************************/
/*                          Deployment Parameters                            */
/*****************************************************************************/

@description('Name of the site resource.')
param siteName string

@description('Display name for the site. Defaults to the resource name.')
param displayName string = siteName

@description('Description of the site.')
param siteDescription string = ''

@description('Country code for the site address (e.g., "US", "DE", "JP").')
param country string

@description('Optional address details.')
param streetAddress1 string = ''
param streetAddress2 string = ''
param city string = ''
param stateOrProvince string = ''
param postalCode string = ''

@description('Labels for categorizing the site.')
param labels object = {}

/*****************************************************************************/
/*                          Site Resource                                    */
/*****************************************************************************/

resource site 'Microsoft.Edge/sites@2025-06-01' = {
  name: siteName
  properties: {
    displayName: displayName
    description: !empty(siteDescription) ? siteDescription : null
    siteAddress: {
      country: country
      streetAddress1: !empty(streetAddress1) ? streetAddress1 : null
      streetAddress2: !empty(streetAddress2) ? streetAddress2 : null
      city: !empty(city) ? city : null
      stateOrProvince: !empty(stateOrProvince) ? stateOrProvince : null
      postalCode: !empty(postalCode) ? postalCode : null
    }
    labels: !empty(labels) ? labels : null
  }
}

/*****************************************************************************/
/*                          Deployment Outputs                               */
/*****************************************************************************/

output site object = {
  id: site.id
  name: site.name
  displayName: site.properties.displayName
}
