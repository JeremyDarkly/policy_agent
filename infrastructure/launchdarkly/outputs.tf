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

// The provider sends tool_keys to the API's `toolKeys` field, which LaunchDarkly
// accepts and ignores -- the tools never reach the served variation. Only the
// `tools: [{key, version}]` field attaches them, and no released provider version
// sends it. This output exposes the intended wiring (with the tool versions the
// API requires) so scripts/attach_ai_config_tools.py can apply it after apply.
output "ai_config_tool_attachments" {
  description = "Intended tool attachments per AI Config, for scripts/attach_ai_config_tools.py."
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
