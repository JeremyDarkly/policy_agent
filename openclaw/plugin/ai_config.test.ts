// Run with: node --test openclaw/plugin/ai_config.test.ts
import assert from "node:assert/strict";
import { test } from "node:test";

import { ConfigTracker, servedConfig } from "./ai_config.ts";

const VARIATION = {
  _ldMeta: { enabled: true, variationKey: "haiku", version: 4, mode: "agent", modelKey: "claude-haiku", modelVersion: 2 },
  model: { name: "claude-haiku-4-5", custom: { openclawModel: "anthropic/claude-haiku-4-5" } },
  provider: { name: "Anthropic" },
  instructions: "Help {{ldctx.name}} ({{ldctx.key}}). Plan: {{ldctx.plan}}.",
  tools: { read: { name: "read", type: "function", parameters: {} } },
};
const CONTEXT = { kind: "user", key: "agent-main", name: "Main" };

test("an enabled variation is served, with its instructions filled from the context", () => {
  const served = servedConfig(VARIATION, CONTEXT);
  assert.ok(served);
  assert.equal(served.config.instructions, "Help Main (agent-main). Plan: .");
  assert.equal(served.config.model.name, "claude-haiku-4-5");
  assert.deepEqual(Object.keys(served.config.tools ?? {}), ["read"]);
  assert.equal(served.meta.variationKey, "haiku");
  assert.equal("_ldMeta" in served.config, false);
});

test("a disabled variation, the evaluation default or a non-object is not served", () => {
  assert.equal(servedConfig({ ...VARIATION, _ldMeta: { enabled: false } }, CONTEXT), undefined);
  assert.equal(servedConfig(null, CONTEXT), undefined);
  assert.equal(servedConfig("x", CONTEXT), undefined);
});

test("a variation without a model is served with an empty model name", () => {
  const { model: _model, ...noModel } = VARIATION;
  const served = servedConfig(noModel, CONTEXT);
  assert.equal(served?.config.model.name, "");
});

test("the tracker sends the AI SDK's events with its track data", () => {
  const sent: [string, unknown, Record<string, unknown>, number][] = [];
  const served = servedConfig(VARIATION, CONTEXT)!;
  const tracker = new ConfigTracker((e, c, d, v) => sent.push([e, c, d as Record<string, unknown>, v]), CONTEXT, "openclaw_agent", served.config, served.meta);

  tracker.trackDuration(900);
  tracker.trackTimeToFirstToken(120);
  tracker.trackTokens({ input: 30, output: 12, total: 42 });
  tracker.trackToolCalls(["read", "exec"]);
  tracker.trackSuccess();
  tracker.trackError();

  assert.deepEqual(sent.map(([e, , , v]) => [e, v]), [
    ["$ld:ai:duration:total", 900], ["$ld:ai:tokens:ttf", 120], ["$ld:ai:tokens:total", 42],
    ["$ld:ai:tokens:input", 30], ["$ld:ai:tokens:output", 12], ["$ld:ai:tool_call", 1], ["$ld:ai:tool_call", 1],
    ["$ld:ai:generation:success", 1], ["$ld:ai:generation:error", 1],
  ]);
  const { runId, ...data } = sent[0][2];
  assert.match(String(runId), /^[0-9a-f-]{36}$/);
  assert.deepEqual(data, {
    configKey: "openclaw_agent", variationKey: "haiku", version: 4, modelName: "claude-haiku-4-5",
    providerName: "Anthropic", modelKey: "claude-haiku", modelVersion: 2,
  });
  assert.equal(sent[6][2].toolKey, "exec");
  assert.equal(sent[0][1], CONTEXT);
});

test("a failure to send doesn't throw", () => {
  const served = servedConfig(VARIATION, CONTEXT)!;
  const tracker = new ConfigTracker(() => { throw new Error("down"); }, CONTEXT, "k", served.config, served.meta);
  tracker.trackSuccess();
});
