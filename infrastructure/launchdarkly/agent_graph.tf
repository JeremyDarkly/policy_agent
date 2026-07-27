# LaunchDarkly AI agent graph mirroring the LangGraph topology in
# app/backend/src/graph/workflow.py. Nodes are AI Configs and root_config_key is
# the entry node (triage). Brand voice is the terminal node, reached from triage
# directly (general questions) or via a specialist hand-off.
#
# Constraint: only `mode = "agent"` configs may be graph nodes. The two
# ai-judge-* configs (mode "judge") are evaluation-only and remain excluded.
#
# NOTE: this is a LaunchDarkly-managed *representation*. The app still
# orchestrates via LangGraph in code and does not read this resource at runtime.
resource "launchdarkly_ai_agent_graph" "banking_agent_graph" {
  project_key = var.project_key
  key         = "banking_agent_graph"
  name        = "Banking Agent Graph"
  description = "Triage routes each query to a specialist agent (accounts, branch, scheduler) or straight to brand voice; specialists hand off to brand voice for the final customer-facing response."

  root_config_key = launchdarkly_ai_config.triage_agent.key

  edges = {
    # Triage -> specialists, plus the direct-to-brand path for general questions.
    triage_to_account = {
      source_config = launchdarkly_ai_config.triage_agent.key
      target_config = launchdarkly_ai_config.account_agent.key
    }
    triage_to_branch = {
      source_config = launchdarkly_ai_config.triage_agent.key
      target_config = launchdarkly_ai_config.branch_agent.key
    }
    triage_to_scheduler = {
      source_config = launchdarkly_ai_config.triage_agent.key
      target_config = launchdarkly_ai_config.scheduler_agent.key
    }
    triage_to_brand = {
      source_config = launchdarkly_ai_config.triage_agent.key
      target_config = launchdarkly_ai_config.brand_agent.key
    }

    # Specialists -> brand voice (final response).
    account_to_brand = {
      source_config = launchdarkly_ai_config.account_agent.key
      target_config = launchdarkly_ai_config.brand_agent.key
    }
    branch_to_brand = {
      source_config = launchdarkly_ai_config.branch_agent.key
      target_config = launchdarkly_ai_config.brand_agent.key
    }
    scheduler_to_brand = {
      source_config = launchdarkly_ai_config.scheduler_agent.key
      target_config = launchdarkly_ai_config.brand_agent.key
    }
  }
}
