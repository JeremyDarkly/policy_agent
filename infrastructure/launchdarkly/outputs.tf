output "tool_count" {
  description = "Number of AI tools managed by Terraform."
  value       = length(local.ai_tool_resources)
}

output "tool_keys" {
  description = "Keys of all managed AI tools."
  value       = sort(keys(local.ai_tool_resources))
}

output "tools" {
  description = "Managed AI tools keyed by tool key, with their LaunchDarkly IDs and versions."
  value = {
    for key, tool in local.ai_tool_resources : key => {
      id      = tool.id
      version = tool.version
    }
  }
}

// Historical: older provider versions sent tool_keys to an API field LaunchDarkly
// accepted and ignored, so the tools never reached the served variation and had to be
// attached out-of-band. Provider v3.1.5 attaches them correctly (verified against a
// live apply -- attached counts match the declared tool_keys), so this output is
// redundant. Kept for now in case anything downstream reads it; the script it once
// fed (scripts/attach_ai_config_tools.py) was never committed to this repo.
output "ai_config_tool_attachments" {
  description = "Redundant as of provider v3.1.5: tool attachments per AI Config, which apply now handles."
  value = {
    for key, variation in local.ai_config_variation_resources : key => {
      variation_key = variation.key
      tools = [
        for tool_key in variation.tool_keys : {
          key     = tool_key
          version = local.ai_tool_resources[tool_key].version
        }
      ]
    }
    if length(variation.tool_keys) > 0
  }
}

output "model_config_count" {
  description = "Number of model configs managed by Terraform."
  value       = length(local.model_config_resources)
}

output "model_config_keys" {
  description = "Keys of all managed model configs."
  value       = sort(keys(local.model_config_resources))
}

output "ai_config_count" {
  description = "Number of AI Configs managed by Terraform."
  value       = length(local.ai_config_resources)
}

output "ai_config_keys" {
  description = "Keys of all managed AI Configs."
  value       = sort(keys(local.ai_config_resources))
}

output "ai_configs" {
  description = "Managed AI Configs keyed by config key, with mode and default variation id."
  value = {
    for key, cfg in local.ai_config_resources : key => {
      mode         = cfg.mode
      variation_id = local.ai_config_variation_resources[key].variation_id
    }
  }
}
