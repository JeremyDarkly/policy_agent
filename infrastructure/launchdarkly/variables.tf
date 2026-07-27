variable "launchdarkly_access_token" {
  description = "LaunchDarkly API access token. Set via TF_VAR_launchdarkly_access_token (the docker-compose 'terraform' service maps LAUNCHDARKLY_ACCESS_TOKEN to it)."
  type        = string
  sensitive   = true
}

variable "project_key" {
  description = "LaunchDarkly project key the AI tools and configs belong to. Required: set via TF_VAR_project_key (the docker-compose 'terraform' service maps LAUNCHDARKLY_PROJECT_KEY from .env to it). No default, so a missing value fails instead of silently targeting the wrong project."
  type        = string

  validation {
    condition     = length(trimspace(var.project_key)) > 0
    error_message = "project_key must be a non-empty value. Set LAUNCHDARKLY_PROJECT_KEY in your .env (mapped to TF_VAR_project_key)."
  }
}

variable "model_override" {
  description = "Optional model id applied to every AI Config variation (mirrors the LD_SETUP_MODEL env var). When null, each config uses its own modelName."
  type        = string
  default     = null
}

variable "target_environment" {
  description = "Environment key where each AI Config's default rule (fallthrough) is turned on and serves its default variation. Sourced from TF_VAR_target_environment (mapped from LAUNCHDARKLY_ENVIRONMENT in .env by the docker-compose terraform service); the default is only a fallback for local runs."
  type        = string
  default     = "test"
}
