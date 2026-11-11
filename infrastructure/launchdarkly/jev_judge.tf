# Jev judge: grades each brand voice reply with Jev (TypeSafe) on five questions
# in one request. See app/backend/src/evaluation/jev_judge.py.
#
# Terraform defines the judge-mode AI Config and its eight metrics. The two
# variations (jev-alongside, jev-only) are created by evals/setup_jev_judge.py:
# they must be on model config TypeSafe.jev-1.14.0 with the questions in
# model.custom, and launchdarkly_ai_config_variation (provider 3.1.5) takes
# either model_config_key or an inline model JSON, with no custom parameters.
#
# Order for a fresh project:
#   terraform apply
#   python3 evals/setup_jev_judge.py --env-file <env>   # variations, pilot rule
#   python3 scripts/set_ai_config_targeting.py --env-file <env>   # default rule -> disabled
#
# In a project where setup_jev_judge.py already created these, import them first:
#   terraform import launchdarkly_ai_config.ai_judge_jev <project>/ai-judge-jev
#   terraform import 'launchdarkly_metric.jev_judge["jev-accuracy"]' <project>/jev-accuracy   (and the other seven)

resource "launchdarkly_ai_config" "ai_judge_jev" {
  project_key = var.project_key
  key         = "ai-judge-jev"
  name        = "Jev judge (several classifications)"
  mode        = "judge"

  # A judge config holds one metric key; the other four classifications keep
  # theirs in the variations' model.custom.questions (#57).
  evaluation_metric_key = "$ld:ai:judge:jev-accuracy"
  # Pinned for the same reason as ai_judge_accuracy (see ai_configs_resources.tf).
  is_inverted = false
  description = "Jev (TypeSafe) grades each brand voice reply on five questions in one request. disabled: LLM judges only. jev-alongside: both. jev-only: Jev replaces the LLM judges."
  tags = [
    "demo",
    "jev-judge",
  ]
}

locals {
  # Same keys, events and directions as METRICS in evals/setup_jev_judge.py.
  jev_judge_metrics = {
    "jev-accuracy" = {
      name        = "Jev: accuracy"
      event_key   = "$ld:ai:judge:jev-accuracy"
      unit        = "score"
      success     = "HigherThanBaseline"
      description = "Jev's grounding score for the brand voice reply, 0-1 (score / 2). Sent on the brand_agent tracker."
    }
    "jev-hallucination-free" = {
      name        = "Jev: hallucination-free"
      event_key   = "$ld:ai:judge:jev-hallucination-free"
      unit        = "score"
      success     = "HigherThanBaseline"
      description = "1 minus Jev's probability that the reply states an unsupported account fact."
    }
    "jev-answers-question" = {
      name        = "Jev: answers the question"
      event_key   = "$ld:ai:judge:jev-answers-question"
      unit        = "score"
      success     = "HigherThanBaseline"
      description = "Jev's probability that the reply addresses what the customer asked."
    }
    "jev-coherence" = {
      name        = "Jev: coherence"
      event_key   = "$ld:ai:judge:jev-coherence"
      unit        = "score"
      success     = "HigherThanBaseline"
      description = "Jev's clarity and organization score for the reply, 0-1 (score / 2)."
    }
    "jev-tone-ok" = {
      name        = "Jev: tone is warm and professional"
      event_key   = "$ld:ai:judge:jev-tone-ok"
      unit        = "score"
      success     = "HigherThanBaseline"
      description = "Jev's probability that the reply's tone is warm_professional."
    }
    "jev-judge-error-rate" = {
      name        = "Jev judge error rate"
      event_key   = "jev-judge-error"
      unit        = "errors"
      success     = "LowerThanBaseline"
      description = "1 when the Jev judge call failed (no key, timeout, 4xx/5xx, missing field), else 0. Sent on every Jev judge call, on the ai-judge-jev context."
    }
    "jev-judge-cost-cents" = {
      name        = "Jev judge cost (cents)"
      event_key   = "jev-judge-cost-cents"
      unit        = "cents"
      success     = "LowerThanBaseline"
      description = "Cost of one Jev judge call in cents: input tokens x the Jev rate."
    }
    "jev-judge-disagreement" = {
      name        = "Jev vs LLM judge disagreement"
      event_key   = "jev-judge-disagreement"
      unit        = "replies"
      success     = "LowerThanBaseline"
      description = "1 when Jev's pass/fail differs from the LLM judges' on accuracy or coherence, else 0. Sent only when jev-alongside is served and both LLM judges ran."
    }
  }
}

resource "launchdarkly_metric" "jev_judge" {
  for_each = local.jev_judge_metrics

  project_key           = var.project_key
  key                   = each.key
  name                  = each.value.name
  description           = each.value.description
  kind                  = "custom"
  is_numeric            = true
  event_key             = each.value.event_key
  unit                  = each.value.unit
  success_criteria      = each.value.success
  unit_aggregation_type = "average"
  analysis_type         = "mean"
  analysis_units        = ["user"]
  tags                  = ["jev-judge"]
}
