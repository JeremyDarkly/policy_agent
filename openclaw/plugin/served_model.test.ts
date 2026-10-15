// Run with: node --test openclaw/plugin/served_model.test.ts
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import { modelIdWithoutRouting, modelsMatch, servedModels } from "./served_model.ts";

const cases = JSON.parse(
  readFileSync(new URL("../../app/backend/tests/model_match_cases.json", import.meta.url), "utf8"),
) as { match: [string, string][]; mismatch: [string, string][] };

test("snapshots and versions of the requested model match", () => {
  for (const [requested, served] of cases.match) assert.ok(modelsMatch(requested, served), `${requested} vs ${served}`);
});

test("different models don't match", () => {
  for (const [requested, served] of cases.mismatch) assert.ok(!modelsMatch(requested, served), `${requested} vs ${served}`);
});

test("modelIdWithoutRouting", () => {
  assert.equal(modelIdWithoutRouting("us.anthropic.claude-3-5-sonnet-20241022-v2:0"), "anthropic.claude-3-5-sonnet-20241022-v2:0");
  assert.equal(modelIdWithoutRouting("openai/GPT-5.4-mini"), "gpt-5.4-mini");
});

// The shape of agent_end.messages from a live run (OpenClaw 2026.10.3, openai/gpt-5.4-mini, one tool call).
const assistant = (responseId: string, stopReason: string) => ({
  role: "assistant",
  api: "openai-responses",
  provider: "openai",
  model: "gpt-5.4-mini",
  responseModel: "gpt-5.4-mini-2026-03-17",
  responseId,
  stopReason,
});

test("only this run's assistant messages count, one entry per served model", () => {
  const earlierTurn = { ...assistant("old", "stop"), responseModel: "gpt-5.4-nano-2026-03-17" };
  const messages = [
    { role: "user" },
    earlierTurn,
    { role: "custom" },
    { role: "user" },
    assistant("r1", "toolUse"),
    { role: "toolResult" },
    assistant("r2", "stop"),
  ];
  assert.deepEqual(servedModels(messages, "openai/gpt-5.4-mini"), [
    { provider: "openai", requested: "openai/gpt-5.4-mini", served: "gpt-5.4-mini-2026-03-17", source: "response" },
  ]);
});

test("without responseModel the message's model is used, and the variation's model is still the request", () => {
  const messages = [{ role: "user" }, { role: "assistant", provider: "anthropic", model: "claude-haiku-4-5", stopReason: "stop" }];
  assert.deepEqual(servedModels(messages, "openai/gpt-5.4-mini"), [
    { provider: "anthropic", requested: "openai/gpt-5.4-mini", served: "claude-haiku-4-5", source: "message_model" },
  ]);
});

test("failed calls and runs with no assistant reply serve nothing", () => {
  assert.deepEqual(servedModels([{ role: "user" }, { ...assistant("x", "error") }]), []);
  assert.deepEqual(servedModels([{ role: "user" }]), []);
  assert.deepEqual(servedModels([]), []);
});

test("without a variation model, the message's own request is used", () => {
  assert.equal(servedModels([{ role: "user" }, assistant("r", "stop")])[0].requested, "gpt-5.4-mini");
});
