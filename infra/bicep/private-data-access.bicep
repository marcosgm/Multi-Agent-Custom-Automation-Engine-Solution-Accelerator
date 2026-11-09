targetScope = 'resourceGroup'

metadata description = 'Side-by-side migration of existing MACAE container apps to private Cosmos DB and Blob connectivity. Does not change public access settings or delete the original apps.'

@description('Existing solution suffix, including its uniqueness suffix.')
param solutionSuffix string

@description('Name of the replacement environment. Use a new name if an earlier failed environment is still being deleted.')
param environmentName string = 'cae-${solutionSuffix}-private'

param location string = resourceGroup().location
param tags object = {}
param cosmosAccountName string
param storageAccountName string
param logAnalyticsWorkspaceResourceId string
param backendAppName string
param mcpAppName string

@description('Exported backend identity, configuration and template. Remove read-only fields and do not include unresolved secret references.')
@secure()
param backendApp object

@description('Exported MCP identity, configuration and template. Remove read-only fields and do not include unresolved secret references.')
@secure()
param mcpApp object

@description('Non-overlapping VNet address space.')
param addressPrefix string = '10.42.0.0/16'
param containerSubnetPrefix string = '10.42.0.0/23'
param privateEndpointSubnetPrefix string = '10.42.2.0/27'

@description('Optional. Fresh container subnet prefix when a failed environment still reserves the original subnet. The original subnet is retained until deletion finishes.')
param replacementContainerSubnetPrefix string = ''

var containerSubnetName = empty(replacementContainerSubnetPrefix) ? 'containers' : 'containers-replacement'

resource nsg 'Microsoft.Network/networkSecurityGroups@2024-05-01' = {
  name: 'nsg-${solutionSuffix}-private'
  location: location
  tags: tags
  properties: {
    securityRules: []
  }
}

resource vnet 'Microsoft.Network/virtualNetworks@2024-05-01' = {
  name: 'vnet-${solutionSuffix}-private'
  location: location
  tags: tags
  properties: {
    addressSpace: {
      addressPrefixes: [addressPrefix]
    }
    subnets: concat([
      {
        name: 'containers'
        properties: {
          addressPrefix: containerSubnetPrefix
          networkSecurityGroup: { id: nsg.id }
          delegations: [
            {
              name: 'container-apps'
              properties: {
                serviceName: 'Microsoft.App/environments'
              }
            }
          ]
        }
      }
      {
        name: 'private-endpoints'
        properties: {
          addressPrefix: privateEndpointSubnetPrefix
          networkSecurityGroup: { id: nsg.id }
          privateEndpointNetworkPolicies: 'Disabled'
        }
      }
    ], empty(replacementContainerSubnetPrefix) ? [] : [
      {
        name: 'containers-replacement'
        properties: {
          addressPrefix: replacementContainerSubnetPrefix
          networkSecurityGroup: { id: nsg.id }
          delegations: [
            {
              name: 'container-apps'
              properties: {
                serviceName: 'Microsoft.App/environments'
              }
            }
          ]
        }
      }
    ])
  }
}

resource cosmos 'Microsoft.DocumentDB/databaseAccounts@2025-10-15' existing = {
  name: cosmosAccountName
}

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' existing = {
  name: storageAccountName
}

var privateLinks = [
  {
    name: 'cosmos'
    resourceId: cosmos.id
    groupId: 'Sql'
    zone: 'privatelink.documents.azure.com'
  }
  {
    name: 'blob'
    resourceId: storage.id
    groupId: 'blob'
    zone: 'privatelink.blob.${az.environment().suffixes.storage}'
  }
]

resource dnsZones 'Microsoft.Network/privateDnsZones@2024-06-01' = [for link in privateLinks: {
  name: link.zone
  location: 'global'
  tags: tags
}]

resource dnsLinks 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = [for (link, i) in privateLinks: {
  parent: dnsZones[i]
  name: 'link-${solutionSuffix}-private'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: { id: vnet.id }
  }
}]

resource endpoints 'Microsoft.Network/privateEndpoints@2024-05-01' = [for link in privateLinks: {
  name: 'pe-${solutionSuffix}-${link.name}'
  location: location
  tags: tags
  properties: {
    subnet: {
      id: '${vnet.id}/subnets/private-endpoints'
    }
    privateLinkServiceConnections: [
      {
        name: link.name
        properties: {
          privateLinkServiceId: link.resourceId
          groupIds: [link.groupId]
        }
      }
    ]
  }
}]

resource dnsGroups 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = [for (link, i) in privateLinks: {
  parent: endpoints[i]
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: link.name
        properties: { privateDnsZoneId: dnsZones[i].id }
      }
    ]
  }
}]

module environment './modules/compute/container-app-environment.bicep' = {
  name: 'private-data-environment'
  params: {
    solutionName: solutionSuffix
    name: environmentName
    location: location
    tags: tags
    logAnalyticsWorkspaceResourceId: logAnalyticsWorkspaceResourceId
    infrastructureSubnetId: '${vnet.id}/subnets/${containerSubnetName}'
  }
}

var backendUrl = 'https://${backendAppName}.${environment.outputs.defaultDomain}'
var mcpUrl = 'https://${mcpAppName}.${environment.outputs.defaultDomain}/mcp'

resource backend 'Microsoft.App/containerApps@2025-07-01' = {
  name: backendAppName
  location: location
  tags: tags
  identity: backendApp.identity
  properties: {
    managedEnvironmentId: environment.outputs.resourceId
    workloadProfileName: backendApp.profile
    configuration: backendApp.configuration
    template: union(backendApp.template, {
      containers: map(backendApp.template.containers, container => union(container, {
        env: map(container.env, variable => variable.name == 'MCP_SERVER_ENDPOINT' ? {
          name: variable.name
          value: mcpUrl
        } : variable)
      }))
    })
  }
  dependsOn: [dnsGroups, dnsLinks]
}

resource mcp 'Microsoft.App/containerApps@2025-07-01' = {
  name: mcpAppName
  location: location
  tags: tags
  identity: mcpApp.identity
  properties: {
    managedEnvironmentId: environment.outputs.resourceId
    workloadProfileName: mcpApp.profile
    configuration: mcpApp.configuration
    template: union(mcpApp.template, {
      containers: map(mcpApp.template.containers, container => union(container, {
        env: map(container.env, variable => variable.name == 'BACKEND_URL' ? {
          name: variable.name
          value: backendUrl
        } : variable)
      }))
    })
  }
  dependsOn: [dnsGroups, dnsLinks]
}

output backendApiUrl string = backendUrl
output mcpServerEndpoint string = mcpUrl
output environmentResourceId string = environment.outputs.resourceId
output virtualNetworkResourceId string = vnet.id
