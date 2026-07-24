// Deployed into the resource group of the existing Log Analytics workspace,
// which may differ from the Logic App resource group (scoped module from main.bicep).
targetScope = 'resourceGroup'

@description('Name of the existing Log Analytics workspace that receives business events.')
param workspaceName string

@description('Custom table for SOCRadar-Taegis bridge business events.')
param tableName string = 'SOCRadarTaegisBridge_CL'

resource workspace 'Microsoft.OperationalInsights/workspaces@2022-10-01' existing = {
  name: workspaceName
}

resource businessEventsTable 'Microsoft.OperationalInsights/workspaces/tables@2022-10-01' = {
  parent: workspace
  name: tableName
  properties: {
    plan: 'Analytics'
    schema: {
      name: tableName
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
}

output tableName string = businessEventsTable.name
