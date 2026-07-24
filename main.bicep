targetScope = 'resourceGroup'

@description('Consumption Logic App name.')
param workflowName string = 'SOCRadar-Taegis-Bridge'

@description('Deployment region.')
param location string = resourceGroup().location

@secure()
@description('SOCRadar Platform API key. Prefer an ARM Key Vault reference in the parameter file.')
param socradarApiKey string

@description('SOCRadar numeric company ID.')
param socradarCompanyId string

@description('SOCRadar API base URL.')
@allowed([
  'https://platform.socradar.com/api'
])
param socradarBaseUrl string = 'https://platform.socradar.com/api'

@description('Taegis API client ID.')
param taegisClientId string

@secure()
@description('Taegis API client secret. Prefer an ARM Key Vault reference in the parameter file.')
param taegisClientSecret string

@description('Taegis tenant ID used in X-Tenant-Context.')
param taegisTenantId string

@allowed([
  'https://api.ctpx.secureworks.com'
  'https://api.delta.taegis.secureworks.com'
  'https://api.foxtrot.taegis.secureworks.com'
  'https://api.echo.taegis.secureworks.com'
  'https://api.golf.taegis.secureworks.com'
])
@description('Documented regional Taegis API base URL.')
param taegisBaseUrl string

@description('Taegis Case assignee ID or mention.')
param taegisAssigneeId string = '@customer'

@allowed([
  'audit'
  'apply'
])
@description('Audit performs reads and plans mutations. Apply performs mutations.')
param syncMode string = 'apply'

@minValue(1)
@maxValue(365)
@description('Reporting window. Lifecycle reconciliation itself performs a full source and target scan.')
param lookbackDays int = 7

@minValue(1)
@maxValue(1440)
@description('Recurrence trigger interval in minutes. In layered mode the Layer 1 incremental window is this interval plus overlapMinutes.')
param recurrenceIntervalMinutes int = 15

@minValue(0)
@maxValue(1440)
@description('Safety overlap in minutes added to the Layer 1 incremental window. Duplicates stay impossible via the deterministic tag and the ledger mapping.')
param overlapMinutes int = 10

@minValue(1)
@maxValue(365)
@description('Layer 0 window in days for the very first run, before the __schedule__ marker exists in the ledger.')
param initialLookbackDays int = 1

@minValue(0)
@maxValue(23)
@description('Layer 2 daily reconciliation runs on the first trigger at or after this UTC hour.')
param reconcileHourUtc int = 6

@minValue(1)
@maxValue(365)
@description('Layer 2 reconciliation window in days. An alarm updated in SOCRadar earlier than this window is only caught by scanStrategy full.')
param reconcileLookbackDays int = 7

@allowed([
  'full'
  'layered'
])
@description('full keeps the lifecycle-complete full scan on every run using an explicit start_date=0 end_date=now epoch window: a dateless SOCRadar request silently degrades to a ~30-day default window (live-verified), so the workflow never issues a dateless call. SOCRadar start_date/end_date filter by creation time (live-verified), so layered mode misses closures of alarms created before reconcileLookbackDays. layered enables the initial/incremental/reconcile epoch windows for high-volume tenants.')
param scanStrategy string = 'layered'

@description('Create and immediately close missing historical closed Cases.')
param backfillClosed bool = false

@description('Alarm IDs allowed to mutate in apply mode. Numeric strings are preferred; JSON numbers are also accepted. Keep one disposable ID during acceptance.')
param applyAlarmIds array = []

@description('Explicit override for all alarms. Never enable during canary acceptance.')
param applyAllAlarms bool = true

@minValue(1)
@maxValue(1000)
@description('Hard ceiling for attempted Taegis mutations in one run.')
param maxMutationsPerRun int = 100

@allowed([
  'manual'
  'auto'
])
@description('manual never touches a closed Taegis Case automatically (default). auto reopens a closed Case when the SOCRadar alarm is open again, behind the same apply-scope and mutation-cap gates as every other mutation; in audit mode it only records planned_reopen.')
param reopenPolicy string = 'manual'

@description('Enable the workflow trigger. Default true for a seamless production install (the workflow starts on its recurrence right after deploy); set false for a staged rollout where you enable it manually.')
param enableWorkflow bool = true

@allowed([
  'CLOSED_CONFIRMED_SECURITY_INCIDENT'
  'CLOSED_AUTHORIZED_ACTIVITY'
  'CLOSED_THREAT_MITIGATED'
  'CLOSED_NOT_VULNERABLE'
  'CLOSED_FALSE_POSITIVE_ALERT'
  'CLOSED_INCONCLUSIVE'
  'CLOSED_INFORMATIONAL'
])
param closeStatusResolved string = 'CLOSED_CONFIRMED_SECURITY_INCIDENT'

@allowed([
  'CLOSED_CONFIRMED_SECURITY_INCIDENT'
  'CLOSED_AUTHORIZED_ACTIVITY'
  'CLOSED_THREAT_MITIGATED'
  'CLOSED_NOT_VULNERABLE'
  'CLOSED_FALSE_POSITIVE_ALERT'
  'CLOSED_INCONCLUSIVE'
  'CLOSED_INFORMATIONAL'
])
param closeStatusMitigated string = 'CLOSED_THREAT_MITIGATED'

@allowed([
  'CLOSED_CONFIRMED_SECURITY_INCIDENT'
  'CLOSED_AUTHORIZED_ACTIVITY'
  'CLOSED_THREAT_MITIGATED'
  'CLOSED_NOT_VULNERABLE'
  'CLOSED_FALSE_POSITIVE_ALERT'
  'CLOSED_INCONCLUSIVE'
  'CLOSED_INFORMATIONAL'
])
param closeStatusFalsePositive string = 'CLOSED_FALSE_POSITIVE_ALERT'

@allowed([
  'CLOSED_CONFIRMED_SECURITY_INCIDENT'
  'CLOSED_AUTHORIZED_ACTIVITY'
  'CLOSED_THREAT_MITIGATED'
  'CLOSED_NOT_VULNERABLE'
  'CLOSED_FALSE_POSITIVE_ALERT'
  'CLOSED_INCONCLUSIVE'
  'CLOSED_INFORMATIONAL'
])
@description('Taegis close status for a future/unknown status returned in the SOCRadar resolved view.')
param closeStatusFallback string = 'CLOSED_INCONCLUSIVE'

@minLength(3)
@maxLength(24)
@description('Storage account for the durable reconciliation ledger. AAD-only; shared key access is disabled.')
param ledgerStorageAccountName string = 'stledger${uniqueString(resourceGroup().id, workflowName)}'

@minValue(1)
@maxValue(3650)
@description('Documented ledger retention in days. Azure Tables have no TTL; cleanup is a documented manual operator task, the workflow never deletes ledger rows.')
param ledgerRetentionDays int = 90

@description('Enable Logic App diagnostic settings.')
param enableDiagnostics bool = false

@description('Create a Log Analytics workspace in this resource group and ingest alarm-level business events into it (table SOCRadarTaegisBridge_CL). Set false to skip, or provide logAnalyticsWorkspaceResourceId to use an existing workspace instead.')
param createLogAnalyticsWorkspace bool = true

@description('Full resource ID of an existing Log Analytics workspace. Overrides createLogAnalyticsWorkspace. The workspace may live in another resource group or subscription.')
param logAnalyticsWorkspaceResourceId string = ''

@description('Delay trigger start to allow a controlled post-deployment review.')
param triggerStartTime string = dateTimeAdd(utcNow(), 'PT3M')

var workflowDefinition = loadJsonContent('workflow.json')
var ledgerMappingTableName = 'SocradarTaegisMapping'
var ledgerEventsTableName = 'SocradarTaegisEvents'
var storageTableDataContributorRoleId = '0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3'
var monitoringMetricsPublisherRoleId = '3913510d-42f4-4e42-8a64-420c390055eb'
var useExistingWorkspace = !empty(logAnalyticsWorkspaceResourceId)
var businessEventsEnabled = useExistingWorkspace || createLogAnalyticsWorkspace
var businessEventsStreamName = 'Custom-SOCRadarTaegisBridge_CL'
var lawSegments = split(logAnalyticsWorkspaceResourceId, '/')
var lawSubscriptionId = length(lawSegments) >= 9 ? lawSegments[2] : subscription().subscriptionId
var lawResourceGroupName = length(lawSegments) >= 9 ? lawSegments[4] : resourceGroup().name
var lawWorkspaceName = length(lawSegments) >= 9 ? lawSegments[8] : 'unused'

resource ledgerStorage 'Microsoft.Storage/storageAccounts@2023-01-01' = {
  name: ledgerStorageAccountName
  location: location
  sku: {
    name: 'Standard_LRS'
  }
  kind: 'StorageV2'
  properties: {
    allowSharedKeyAccess: false
    allowBlobPublicAccess: false
    supportsHttpsTrafficOnly: true
    minimumTlsVersion: 'TLS1_2'
    accessTier: 'Hot'
  }
}

resource ledgerTableService 'Microsoft.Storage/storageAccounts/tableServices@2023-01-01' = {
  parent: ledgerStorage
  name: 'default'
}

resource ledgerMappingTable 'Microsoft.Storage/storageAccounts/tableServices/tables@2023-01-01' = {
  parent: ledgerTableService
  name: ledgerMappingTableName
}

resource ledgerEventsTable 'Microsoft.Storage/storageAccounts/tableServices/tables@2023-01-01' = {
  parent: ledgerTableService
  name: ledgerEventsTableName
}

resource workflow 'Microsoft.Logic/workflows@2019-05-01' = {
  name: workflowName
  location: location
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    state: enableWorkflow ? 'Enabled' : 'Disabled'
    definition: workflowDefinition
    parameters: {
      SocradarApiKey: {
        value: socradarApiKey
      }
      SocradarCompanyId: {
        value: socradarCompanyId
      }
      SocradarBaseUrl: {
        value: socradarBaseUrl
      }
      TaegisClientId: {
        value: taegisClientId
      }
      TaegisClientSecret: {
        value: taegisClientSecret
      }
      TaegisTenantId: {
        value: taegisTenantId
      }
      TaegisBaseUrl: {
        value: taegisBaseUrl
      }
      TaegisAssigneeId: {
        value: taegisAssigneeId
      }
      SyncMode: {
        value: syncMode
      }
      LookbackDays: {
        value: lookbackDays
      }
      RecurrenceIntervalMinutes: {
        value: recurrenceIntervalMinutes
      }
      OverlapMinutes: {
        value: overlapMinutes
      }
      InitialLookbackDays: {
        value: initialLookbackDays
      }
      ReconcileHourUtc: {
        value: reconcileHourUtc
      }
      ReconcileLookbackDays: {
        value: reconcileLookbackDays
      }
      ScanStrategy: {
        value: scanStrategy
      }
      BackfillClosed: {
        value: backfillClosed
      }
      ApplyAlarmIds: {
        value: applyAlarmIds
      }
      ApplyAllAlarms: {
        value: applyAllAlarms
      }
      MaxMutationsPerRun: {
        value: maxMutationsPerRun
      }
      ReopenPolicy: {
        value: reopenPolicy
      }
      CloseStatusResolved: {
        value: closeStatusResolved
      }
      CloseStatusMitigated: {
        value: closeStatusMitigated
      }
      CloseStatusFalsePositive: {
        value: closeStatusFalsePositive
      }
      CloseStatusFallback: {
        value: closeStatusFallback
      }
      TriggerStartTime: {
        value: triggerStartTime
      }
      LedgerTableEndpoint: {
        value: ledgerStorage.properties.primaryEndpoints.table
      }
      LedgerMappingTableName: {
        value: ledgerMappingTableName
      }
      LedgerEventsTableName: {
        value: ledgerEventsTableName
      }
      LedgerRetentionDays: {
        value: ledgerRetentionDays
      }
      LogIngestionEndpoint: {
        value: businessEventsEnabled ? businessEventsDce!.properties.logsIngestion.endpoint : ''
      }
      DcrImmutableId: {
        value: businessEventsEnabled ? businessEventsDcr!.properties.immutableId : ''
      }
      StreamName: {
        value: businessEventsEnabled ? businessEventsStreamName : ''
      }
    }
  }
}

resource ledgerRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(ledgerStorage.id, workflowName, storageTableDataContributorRoleId)
  scope: ledgerStorage
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageTableDataContributorRoleId)
    principalId: workflow.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource diagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = if (enableDiagnostics && !empty(logAnalyticsWorkspaceResourceId)) {
  name: '${workflowName}-diagnostics'
  scope: workflow
  properties: {
    workspaceId: logAnalyticsWorkspaceResourceId
    logs: [
      {
        categoryGroup: 'allLogs'
        enabled: true
      }
    ]
    metrics: [
      {
        category: 'AllMetrics'
        enabled: true
      }
    ]
  }
}

resource builtinWorkspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' = if (createLogAnalyticsWorkspace && !useExistingWorkspace) {
  name: '${workflowName}-law'
  location: location
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
  }
}

module businessEventsTable 'modules/law-table.bicep' = if (businessEventsEnabled) {
  name: 'socradar-taegis-business-events-table'
  scope: resourceGroup(lawSubscriptionId, lawResourceGroupName)
  params: {
    workspaceName: useExistingWorkspace ? lawWorkspaceName : '${workflowName}-law'
  }
  dependsOn: [
    builtinWorkspace
  ]
}

resource businessEventsDce 'Microsoft.Insights/dataCollectionEndpoints@2022-06-01' = if (businessEventsEnabled) {
  name: '${workflowName}-dce'
  location: location
  properties: {
    networkAcls: {
      publicNetworkAccess: 'Enabled'
    }
  }
}

resource businessEventsDcr 'Microsoft.Insights/dataCollectionRules@2022-06-01' = if (businessEventsEnabled) {
  name: '${workflowName}-dcr'
  location: location
  properties: {
    dataCollectionEndpointId: businessEventsDce.id
    streamDeclarations: {
      'Custom-SOCRadarTaegisBridge_CL': {
        columns: [
          { name: 'TimeGenerated', type: 'datetime' }
          { name: 'RunId', type: 'string' }
          { name: 'WindowMode', type: 'string' }
          { name: 'CompanyId', type: 'string' }
          { name: 'AlarmId', type: 'string' }
          { name: 'EventType', type: 'string' }
          { name: 'CaseId', type: 'string' }
          { name: 'Detail', type: 'string' }
        ]
      }
    }
    destinations: {
      logAnalytics: [
        {
          workspaceResourceId: useExistingWorkspace ? logAnalyticsWorkspaceResourceId : builtinWorkspace!.id
          name: 'businessEventsWorkspace'
        }
      ]
    }
    dataFlows: [
      {
        streams: [
          businessEventsStreamName
        ]
        destinations: [
          'businessEventsWorkspace'
        ]
        transformKql: 'source'
        outputStream: businessEventsStreamName
      }
    ]
  }
  dependsOn: [
    businessEventsTable
  ]
}

// Monitoring Metrics Publisher is granted on the DCR, not the DCE
// (proven pattern from the SOCRadar Feeds solution).
resource businessEventsDcrRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (businessEventsEnabled) {
  name: guid(resourceGroup().id, '${workflowName}-dcr', monitoringMetricsPublisherRoleId)
  scope: businessEventsDcr
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', monitoringMetricsPublisherRoleId)
    principalId: workflow.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

output logicAppName string = workflow.name
output logicAppResourceId string = workflow.id
output deployedState string = enableWorkflow ? 'Enabled' : 'Disabled'
output synchronizationMode string = syncMode
output ledgerStorageAccount string = ledgerStorage.name
output ledgerTableEndpoint string = ledgerStorage.properties.primaryEndpoints.table
output ledgerRetentionDaysDocumented int = ledgerRetentionDays
output businessEventsConfigured bool = businessEventsEnabled
