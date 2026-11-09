// ============================================================================
// Module: Azure Container Apps Environment
// Description: Creates an Azure Container Apps managed environment
// API: Microsoft.App/managedEnvironments@2024-03-01
// ============================================================================

@description('Solution name used for naming convention.')
param solutionName string

@description('Name of the Container Apps Environment.')
param name string = 'cae-${solutionName}'

@description('Azure region for deployment.')
param location string

@description('Resource tags.')
param tags object = {}

@description('Resource ID of the Log Analytics workspace.')
param logAnalyticsWorkspaceResourceId string

@description('Enable zone redundancy.')
param zoneRedundant bool = false

@description('Optional. Dedicated subnet delegated to Microsoft.App/environments. Must be set when the environment is created.')
param infrastructureSubnetId string = ''

@description('Workload profiles configuration (e.g., Consumption or dedicated D4 profiles).')
param workloadProfiles array = [
  {
    name: 'Consumption'
    workloadProfileType: 'Consumption'
  }
]

// ============================================================================
// Resource Deployment
// ============================================================================
resource containerAppEnvironment 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: name
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: reference(logAnalyticsWorkspaceResourceId, '2023-09-01').customerId
        sharedKey: listKeys(logAnalyticsWorkspaceResourceId, '2023-09-01').primarySharedKey
      }
    }
    workloadProfiles: workloadProfiles
    zoneRedundant: zoneRedundant
    vnetConfiguration: empty(infrastructureSubnetId) ? null : {
      infrastructureSubnetId: infrastructureSubnetId
      internal: false
    }
  }
}

// ============================================================================
// Outputs
// ============================================================================
@description('The name of the Container Apps Environment.')
output name string = containerAppEnvironment.name

@description('The resource ID of the Container Apps Environment.')
output resourceId string = containerAppEnvironment.id

@description('The default domain of the Container Apps Environment.')
output defaultDomain string = containerAppEnvironment.properties.defaultDomain

@description('The static IP address of the Container Apps Environment.')
output staticIp string = containerAppEnvironment.properties.staticIp
