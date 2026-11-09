targetScope = 'resourceGroup'

@description('Name of the manually triggered full post-deployment seeding job.')
param name string
param location string = resourceGroup().location
param environmentResourceId string
param managedIdentityResourceId string
param managedIdentityClientId string
param registryServer string
param image string
param storageAccountName string
param searchEndpoint string
param backendUrl string
param projectEndpoint string
param projectResourceId string
param openaiEndpoint string
param tags object = {}

resource job 'Microsoft.App/jobs@2025-07-01' = {
  name: name
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${managedIdentityResourceId}': {}
    }
  }
  properties: {
    environmentId: environmentResourceId
    workloadProfileName: 'Consumption'
    configuration: {
      triggerType: 'Manual'
      replicaTimeout: 1800
      replicaRetryLimit: 0
      manualTriggerConfig: {
        parallelism: 1
        replicaCompletionCount: 1
      }
      registries: [
        {
          server: registryServer
          identity: managedIdentityResourceId
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'seed'
          image: image
          resources: {
            cpu: 1
            memory: '2Gi'
          }
          env: [
            {
              name: 'AZURE_CLIENT_ID'
              value: managedIdentityClientId
            }
            {
              name: 'AZURE_STORAGE_ACCOUNT_NAME'
              value: storageAccountName
            }
            {
              name: 'AZURE_AI_SEARCH_ENDPOINT'
              value: searchEndpoint
            }
            {
              name: 'BACKEND_URL'
              value: backendUrl
            }
            {
              name: 'AZURE_AI_PROJECT_ENDPOINT'
              value: projectEndpoint
            }
            {
              name: 'AZURE_AI_PROJECT_RESOURCE_ID'
              value: projectResourceId
            }
            {
              name: 'AZURE_OPENAI_ENDPOINT'
              value: openaiEndpoint
            }
          ]
        }
      ]
    }
  }
}

output jobName string = job.name
