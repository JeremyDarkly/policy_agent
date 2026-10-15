// Which model served each of a run's model calls. Matches app/backend/src/utils/served_model.py; both are tested
// against app/backend/tests/model_match_cases.json.
//
// OpenClaw's hook events carry the model it requested. The model the API reported is on each assistant message as
// `responseModel`. OpenAI Responses and Anthropic set it on every reply; OpenAI Completions, Mistral and Google set
// it only when it differs from the request; CLI-backed runs never set it.

export const SERVED_EVENT = "ai-model-served";
export const MISMATCH_EVENT = "ai-model-mismatch";

// `response`: the API named the model. `message_model`: it didn't, so this is the model OpenClaw requested.
export type ServedSource = "response" | "message_model";

export type ServedModel = { provider?: string; requested: string; served: string; source: ServedSource };

const PROFILE_PREFIX = /^(us|us-gov|eu|apac|jp|au|ca|global)\./;
// Suffixes that make a model ID more specific: a snapshot date or a version.
const VERSION_SUFFIXES = [/:\d+$/, /-v\d+$/, /-\d{8}$/, /-\d{4}-\d{2}-\d{2}$/];
const LATEST = /-latest$/;

// A model ID without the parts that only say how it was reached: an ARN path or `provider/` reference, and a
// Bedrock cross-region profile prefix.
export function modelIdWithoutRouting(modelId: string): string {
  const model = (modelId ?? "").trim().toLowerCase();
  return model.slice(model.lastIndexOf("/") + 1).replace(PROFILE_PREFIX, "");
}

// `model`, then `model` with its trailing snapshot or version suffixes removed one at a time.
function lessSpecific(model: string): string[] {
  const forms = [model];
  for (let suffix = VERSION_SUFFIXES.find((re) => re.test(model)); suffix; suffix = VERSION_SUFFIXES.find((re) => re.test(model))) {
    model = model.replace(suffix, "");
    forms.push(model);
  }
  return forms;
}

// True when the served model is the requested one, or the requested one made more specific. An alias may resolve to
// a snapshot (gpt-5.4-mini -> gpt-5.4-mini-2026-03-17), but a pinned snapshot or version must be served exactly.
// OpenClaw itself logs an alias resolving to a snapshot as a model route change; this doesn't count it.
export function modelsMatch(requested: string, served: string): boolean {
  const wanted = modelIdWithoutRouting(requested).replace(LATEST, "");
  return lessSpecific(modelIdWithoutRouting(served).replace(LATEST, "")).includes(wanted);
}

type AssistantMessage = { role: "assistant"; provider?: string; model?: string; responseModel?: string; stopReason?: string };

// The model that served each of this run's model calls (one entry per assistant message), from agent_end's messages.
// Those include earlier turns of the session, so only assistant messages after the last user message count.
// `requested` is the variation's model; without one, each message's own requested model is used.
export function servedCalls(messages: unknown[], requested?: string): ServedModel[] {
  const lastUser = messages.findLastIndex((message) => (message as { role?: string })?.role === "user");
  const calls: ServedModel[] = [];
  for (const message of messages.slice(lastUser + 1)) {
    const assistant = message as AssistantMessage;
    if (assistant?.role !== "assistant" || assistant.stopReason === "error" || assistant.stopReason === "aborted") continue;
    const responseModel = assistant.responseModel?.trim();
    const served = responseModel || assistant.model?.trim();
    if (!served) continue;
    calls.push({
      provider: assistant.provider,
      requested: requested || assistant.model || "",
      served,
      source: responseModel ? "response" : "message_model",
    });
  }
  return calls;
}
