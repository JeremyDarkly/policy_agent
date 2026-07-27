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

# Keyed map of the model configs above, consumed by outputs.tf.
locals {
  model_config_resources = {
    for r in [
      launchdarkly_model_config.claude_sonnet_5,
      launchdarkly_model_config.llama_3_1_70b_accounts,
      launchdarkly_model_config.claude_haiku_4_5_branch,
      launchdarkly_model_config.nova_pro,
      launchdarkly_model_config.claude_haiku_4_5_brand_voice,
      launchdarkly_model_config.claude_sonnet_4,
    ] : r.key => r
  }
}
