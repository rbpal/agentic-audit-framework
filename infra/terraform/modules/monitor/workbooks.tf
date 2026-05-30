# Application Insights Workbooks for the Layer-3 agent stack.
#
# Each workbook is defined as a JSON file under infra/azure/workbooks/ and
# wrapped in an azurerm_application_insights_workbook resource here. Keeping
# the JSON in its own file (rather than inline) means dashboards can be
# round-tripped through the Azure Portal authoring UI without churning
# Terraform diffs — export from Portal, overwrite the .json file, terraform
# apply.
#
# The Terraform `name` argument must be a stable UUID (Azure uses it as the
# workbook's permanent identifier). Hardcoded UUIDs are deliberate — letting
# Terraform regenerate them would destroy and re-create the workbook on every
# apply, losing pinned-to-dashboard links.

resource "azurerm_application_insights_workbook" "agent_success" {
  name                = "a4f10002-0001-4002-a001-000000000001"
  resource_group_name = var.resource_group_name
  location            = var.location

  display_name = "Agent Success"
  description  = "Layer-3 supervisor health — success rate, tool distribution, P95 iterations, escalation rate."

  source_id = lower(azurerm_application_insights.this.id)
  category  = "workbook"

  data_json = file("${path.module}/../../../azure/workbooks/agent_success.json")

  tags = var.tags
}

resource "azurerm_application_insights_workbook" "cost_tokens" {
  name                = "a4f10003-0002-4002-a001-000000000002"
  resource_group_name = var.resource_group_name
  location            = var.location

  display_name = "Cost & Tokens"
  description  = "LLM spend health — cumulative cost, token throughput, cost per agent run, tokens by model."

  source_id = lower(azurerm_application_insights.this.id)
  category  = "workbook"

  data_json = file("${path.module}/../../../azure/workbooks/cost_tokens.json")

  tags = var.tags
}

resource "azurerm_application_insights_workbook" "errors" {
  name                = "a4f10004-0003-4002-a001-000000000003"
  resource_group_name = var.resource_group_name
  location            = var.location

  display_name = "Errors & Hallucinations"
  description  = "Failure health — error rate by layer, hallucination rate, structural-error scenarios, recent failures."

  source_id = lower(azurerm_application_insights.this.id)
  category  = "workbook"

  data_json = file("${path.module}/../../../azure/workbooks/errors.json")

  tags = var.tags
}
