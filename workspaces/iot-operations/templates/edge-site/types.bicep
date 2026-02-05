metadata description = 'Shared type definitions for Azure Edge site resources.'

/*****************************************************************************/
/*                          Exported Types                                   */
/*****************************************************************************/

@export()
type siteAddressType = {
  @description('Country code for the site address (e.g., "US", "DE", "JP").')
  country: string?

  @description('Primary street address.')
  streetAddress1: string?

  @description('Secondary street address.')
  streetAddress2: string?

  @description('City name.')
  city: string?

  @description('State or province.')
  stateOrProvince: string?

  @description('Postal code.')
  postalCode: string?
}
