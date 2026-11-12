# Reusable model configs. Each variation points at one of these via
# `model_config_key` instead of carrying an inline `model` JSON blob, so the
# model id, sampling params, and provider are defined once and shared.
# LaunchDarkly resolves them into the `model` object the SDK receives
# (model.name / model.parameters / provider.name are populated from these).
#
# var.model_override (LD_SETUP_MODEL) still overrides the model id everywhere.
# NOTE: every argument here except `tags` is ForceNew in the provider, so an
# edit replaces the model config rather than updating it in place.

resource "launchdarkly_model_config" "claude_sonnet_5" {
  project_key    = var.project_key
  key            = "claude-sonnet-5"
  name           = "Claude Sonnet 5"
  model_id       = var.model_override != null ? var.model_override : "us.anthropic.claude-sonnet-5"
  model_provider = "Bedrock"

  tags = ["managed-by-terraform"]
}

resource "launchdarkly_model_config" "llama_3_1_70b_accounts" {
  project_key    = var.project_key
  key            = "llama-3-1-70b-accounts"
  name           = "Llama 3.1 70B (Accounts)"
  model_id       = var.model_override != null ? var.model_override : "us.meta.llama3-1-70b-instruct-v1:0"
  model_provider = "Bedrock"

  params = jsonencode({
    temperature = 0.1
  })

  tags = ["managed-by-terraform"]
}

resource "launchdarkly_model_config" "claude_haiku_4_5_branch" {
  project_key    = var.project_key
  key            = "claude-haiku-4-5-branch"
  name           = "Claude Haiku 4.5 (Branch)"
  model_id       = var.model_override != null ? var.model_override : "us.anthropic.claude-haiku-4-5-20251001-v1:0"
  model_provider = "Bedrock"

  params = jsonencode({
    temperature = 0.1
  })

  tags = ["managed-by-terraform"]
}

resource "launchdarkly_model_config" "nova_pro" {
  project_key    = var.project_key
  key            = "nova-pro"
  name           = "Amazon Nova Pro"
  model_id       = var.model_override != null ? var.model_override : "us.amazon.nova-pro-v1:0"
  model_provider = "Bedrock"

  tags = ["managed-by-terraform"]
}

resource "launchdarkly_model_config" "claude_haiku_4_5_brand_voice" {
  project_key    = var.project_key
  key            = "claude-haiku-4-5-brand-voice"
  name           = "Claude Haiku 4.5 (Brand Voice)"
  model_id       = var.model_override != null ? var.model_override : "us.anthropic.claude-haiku-4-5-20251001-v1:0"
  model_provider = "Bedrock"

  params = jsonencode({
    max_tokens  = 10000
    temperature = 0.9
    top_k       = 250
    top_p       = 1
  })

  tags = ["managed-by-terraform"]
}

resource "launchdarkly_model_config" "claude_sonnet_4" {
  project_key    = var.project_key
  key            = "claude-sonnet-4"
  name           = "Claude Sonnet 4"
  model_id       = var.model_override != null ? var.model_override : "us.anthropic.claude-sonnet-4-20250514-v1:0"
  model_provider = "Bedrock"

  params = jsonencode({
    temperature = 0.1
  })

  tags = ["managed-by-terraform"]
}

# Gemini 4 Carbon on the Gemini API (issue #79), for the judges' and account_agent's
# gemini-4-carbon variations. The backend picks ChatGoogleGenerativeAI from the "Gemini"
# provider (GEMINI_PROVIDERS in app/backend/src/utils/llm_config.py) and needs GOOGLE_API_KEY.
# Not affected by model_override, which is a Bedrock model ID.
# max_tokens: Carbon thinks before answering, and its reasoning tokens count against the
# limit (about 350-510 on short prompts, measured 2026-11-12); the backend default is 2000.
# Costs are the list price, $4 / $20 per 1M tokens, which applies from 2026-12-01 (the
# introductory $2 / $10 ends on 2026-11-30), so Monitoring doesn't understate the cost.
resource "launchdarkly_model_config" "gemini_4_carbon" {
  project_key    = var.project_key
  key            = "gemini-4-carbon"
  name           = "Gemini 4 Carbon"
  model_id       = "gemini-4-carbon"
  model_provider = "Gemini"

  cost_per_input_token  = 0.000004
  cost_per_output_token = 0.00002

  params = jsonencode({
    max_tokens = 8000
  })

  tags = ["managed-by-terraform"]
}

# Azure AI Foundry deployment for branch_agent's azure-foundry variation (issue #67).
# The global model catalog has no Azure provider, so this is a project model config with a
# free-text provider. The backend picks its Azure client from that provider string
# (AZURE_PROVIDERS in app/backend/src/utils/llm_config.py). model_id is the deployment name;
# baseModel is the model the deployment runs, which ai-model-mismatch compares the served
# model with. Not affected by model_override. No costs: the Monitoring tab shows none (#69).
resource "launchdarkly_model_config" "azure_foundry_branch" {
  count = local.azure_foundry_enabled ? 1 : 0

  project_key    = var.project_key
  key            = "azure-foundry-branch"
  name           = "Azure AI Foundry (Branch)"
  model_id       = var.azure_foundry_branch_deployment
  model_provider = "Azure AI Foundry"

  custom_parameters = local.azure_foundry_base_model == "" ? null : jsonencode({
    baseModel = local.azure_foundry_base_model
  })

  params = jsonencode({
    temperature = 0.1
  })

  tags = ["managed-by-terraform"]

  # Once created, an empty AZURE_FOUNDRY_DEPLOYMENT (for example, a run without the Azure
  # variables loaded) would plan to delete this config and the azure-foundry variation, even
  # while targeting serves that variation. prevent_destroy makes that plan fail instead.
  # To remove the Azure resources on purpose, see "Removing the Azure resources" in the README.
  lifecycle {
    prevent_destroy = true
  }
}

locals {
  # Null and "" both mean unset. coalesce() can't be used here: it errors when every
  # argument is null or "", which is what docker-compose passes for an unset env var.
  azure_foundry_enabled    = trimspace(var.azure_foundry_branch_deployment == null ? "" : var.azure_foundry_branch_deployment) != ""
  azure_foundry_base_model = trimspace(var.azure_foundry_branch_base_model == null ? "" : var.azure_foundry_branch_base_model)

  # Keyed map of the model configs above, consumed by outputs.tf.
  model_config_resources = {
    for r in concat([
      launchdarkly_model_config.claude_sonnet_5,
      launchdarkly_model_config.llama_3_1_70b_accounts,
      launchdarkly_model_config.claude_haiku_4_5_branch,
      launchdarkly_model_config.nova_pro,
      launchdarkly_model_config.claude_haiku_4_5_brand_voice,
      launchdarkly_model_config.claude_sonnet_4,
      launchdarkly_model_config.gemini_4_carbon,
    ], launchdarkly_model_config.azure_foundry_branch) : r.key => r
  }
}
