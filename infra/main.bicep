// Helios Azure infrastructure (Phase 10).
// Provisions: Container Apps env + app, PostgreSQL Flexible Server (the
// warehouse), a Blob storage account (MLflow artifacts / reports), Key Vault
// (secrets), and a Container Registry. Secrets are never inlined — the DB
// password is passed as a secure parameter and stored in Key Vault; the app
// reads it via a Key Vault secret reference, so no credential lands in the repo.
//
// Deploy:
//   az group create -n helios-rg -l canadacentral
//   az deployment group create -g helios-rg -f infra/main.bicep \
//     -p namePrefix=helios pgAdminPassword=$PG_PASSWORD

@description('Short prefix for all resource names')
param namePrefix string = 'helios'

@description('Location for all resources')
param location string = resourceGroup().location

@description('PostgreSQL administrator password')
@secure()
param pgAdminPassword string

@description('Container image (registry/name:tag). Set by CI after push.')
param containerImage string = 'mcr.microsoft.com/k8se/quickstart:latest'

var pgAdmin = 'heliosadmin'
var tags = { project: 'helios', managedBy: 'bicep' }

// --- Container Registry ---------------------------------------------------
resource acr 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: '${namePrefix}acr${uniqueString(resourceGroup().id)}'
  location: location
  tags: tags
  sku: { name: 'Basic' }
  properties: { adminUserEnabled: true }
}

// --- Blob storage (MLflow artifacts, generated reports) -------------------
resource storage 'Microsoft.Storage/storageAccounts@2023-01-01' = {
  name: '${namePrefix}st${uniqueString(resourceGroup().id)}'
  location: location
  tags: tags
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    supportsHttpsTrafficOnly: true
  }
}

// --- PostgreSQL Flexible Server (the warehouse) ---------------------------
resource pg 'Microsoft.DBforPostgreSQL/flexibleServers@2023-06-01-preview' = {
  name: '${namePrefix}-pg-${uniqueString(resourceGroup().id)}'
  location: location
  tags: tags
  sku: { name: 'Standard_B1ms', tier: 'Burstable' }
  properties: {
    version: '16'
    administratorLogin: pgAdmin
    administratorLoginPassword: pgAdminPassword
    storage: { storageSizeGB: 32 }
    backup: { backupRetentionDays: 7, geoRedundantBackup: 'Disabled' }
  }
}

resource pgDb 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2023-06-01-preview' = {
  parent: pg
  name: 'helios'
}

// Allow Azure services (Container Apps) to reach the DB.
resource pgFirewall 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2023-06-01-preview' = {
  parent: pg
  name: 'AllowAzureServices'
  properties: { startIpAddress: '0.0.0.0', endIpAddress: '0.0.0.0' }
}

// --- Key Vault (secrets) --------------------------------------------------
resource kv 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: '${namePrefix}-kv-${uniqueString(resourceGroup().id)}'
  location: location
  tags: tags
  properties: {
    sku: { family: 'A', name: 'standard' }
    tenantId: subscription().tenantId
    enableRbacAuthorization: true
    enableSoftDelete: true
  }
}

resource kvDbUrl 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: kv
  name: 'helios-warehouse-url'
  properties: {
    value: 'postgresql+psycopg://${pgAdmin}:${pgAdminPassword}@${pg.properties.fullyQualifiedDomainName}:5432/helios?sslmode=require'
  }
}

// --- Container Apps environment + app ------------------------------------
resource logs 'Microsoft.OperationalInsights/workspaces@2022-10-01' = {
  name: '${namePrefix}-logs'
  location: location
  tags: tags
  properties: { sku: { name: 'PerGB2018' }, retentionInDays: 30 }
}

resource caenv 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: '${namePrefix}-env'
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logs.properties.customerId
        sharedKey: logs.listKeys().primarySharedKey
      }
    }
  }
}

resource api 'Microsoft.App/containerApps@2024-03-01' = {
  name: '${namePrefix}-api'
  location: location
  tags: tags
  identity: { type: 'SystemAssigned' }  // used to read Key Vault via RBAC
  properties: {
    managedEnvironmentId: caenv.id
    configuration: {
      ingress: { external: true, targetPort: 8080, transport: 'auto' }
      registries: [ { server: acr.properties.loginServer, identity: 'system' } ]
    }
    template: {
      containers: [
        {
          name: 'helios-api'
          image: containerImage
          resources: { cpu: json('0.5'), memory: '1Gi' }
          env: [
            { name: 'PORT', value: '8080' }
            { name: 'HELIOS_WAREHOUSE_URL', secretRef: 'warehouse-url' }
          ]
          probes: [
            { type: 'Liveness', httpGet: { path: '/health', port: 8080 }, periodSeconds: 30 }
            { type: 'Readiness', httpGet: { path: '/health', port: 8080 }, periodSeconds: 10 }
          ]
        }
      ]
      scale: { minReplicas: 1, maxReplicas: 3 }
    }
  }
}

output acrLoginServer string = acr.properties.loginServer
output apiFqdn string = api.properties.configuration.ingress.fqdn
output keyVaultName string = kv.name
