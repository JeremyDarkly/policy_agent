// The run's AgentControl config and its metrics, on the LaunchDarkly AI SDK (@launchdarkly/ai-node 1.0.0).
//
// The SDK's config() calls the model itself. Here OpenClaw calls the model, so the plugin reads the variation with
// its own LaunchDarkly client and records the SDK's $ld:ai:* events with the SDK's track data (makeRunTrackData).
// The SDK's inspectConfig() isn't used: it needs initClient(), which registers a global OpenTelemetry tracer
// provider and would export the gateway's own spans to LaunchDarkly.
//
// Time to first token ($ld:ai:tokens:ttf) is still sent: the 1.0.0 SDK no longer sends it, but the config's
// Monitoring tab still charts it.
import { makeRunTrackData, parseTemplate, type AiConfigRep, type LDVariationMeta, type TrackData } from "@launchdarkly/ai-node";

export type AgentConfig = AiConfigRep;

type Track = (event: string, context: unknown, data: unknown, value: number) => void;

// The served config, or undefined when the variation is disabled or isn't an AI Config (an unknown key comes back
// as the evaluation default). `{{ldctx.attribute}}` placeholders in the instructions are filled from the context,
// and ones it has no value for are left empty, as the earlier AI SDK rendered them.
export function servedConfig(value: unknown, context: object): { config: AgentConfig; meta: LDVariationMeta } | undefined {
  if (!value || typeof value !== "object") return undefined;
  const { _ldMeta: meta, ...raw } = value as Record<string, unknown> & { _ldMeta?: LDVariationMeta };
  if (!meta?.enabled) return undefined;
  const config = raw as unknown as AgentConfig;
  const model = config.model && typeof config.model.name === "string" ? config.model : { ...config.model, name: "" };
  const provider = config.provider && typeof config.provider.name === "string" ? config.provider : { name: "" };
  const instructions = typeof config.instructions === "string"
    ? parseTemplate(config.instructions, { ldctx: context }).replace(/\{\{[\w.]+\}\}/g, "")
    : undefined;
  return { config: { ...config, model, provider, instructions }, meta };
}

// One run's metrics on its config. A failure to send never fails the run.
export class ConfigTracker {
  readonly trackData: TrackData;
  private readonly track: Track;
  private readonly context: unknown;

  constructor(track: Track, context: unknown, configKey: string, config: AgentConfig, meta: LDVariationMeta) {
    this.track = track;
    this.context = context;
    const { environmentId, ...data } = makeRunTrackData({ configKey, config, meta });
    this.trackData = (environmentId ? { ...data, environmentId } : data) as TrackData;
  }

  private send(event: string, value: number, extra?: Record<string, unknown>) {
    try {
      this.track(event, this.context, { ...this.trackData, ...extra }, value);
    } catch {
      // Reporting is best effort.
    }
  }

  trackDuration(ms: number) { this.send("$ld:ai:duration:total", ms); }
  trackTimeToFirstToken(ms: number) { this.send("$ld:ai:tokens:ttf", ms); }
  trackSuccess() { this.send("$ld:ai:generation:success", 1); }
  trackError() { this.send("$ld:ai:generation:error", 1); }

  trackTokens({ input, output, total }: { input: number; output: number; total: number }) {
    if (total > 0) this.send("$ld:ai:tokens:total", total);
    if (input > 0) this.send("$ld:ai:tokens:input", input);
    if (output > 0) this.send("$ld:ai:tokens:output", output);
  }

  trackToolCalls(toolKeys: string[]) {
    for (const toolKey of toolKeys) this.send("$ld:ai:tool_call", 1, { toolKey });
  }
}
