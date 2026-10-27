# Adding LaunchDarkly to an existing AI agent

A routing agent that classifies customer messages, before and after adding the LaunchDarkly AI SDK.
The change adds 20 lines and edits 4.

- [`before.py`](before.py): model and prompt are hardcoded
- [`after.py`](after.py): model and prompt come from the `triage_agent` AI Config, and each call reports its metrics

## The diff

```diff
 import json
+import os
 
 import boto3
+import ldclient
+from ldai import AIAgentConfigDefault, LDAIClient, ModelConfig
+from ldai.tracker import TokenUsage
+from ldclient import Context
+from ldclient.config import Config
 
 bedrock = boto3.client("bedrock-runtime")
+ldclient.set_config(Config(os.environ["LAUNCHDARKLY_SDK_KEY"]))
+ai_client = LDAIClient(ldclient.get())
 
 MODEL_ID = "us.anthropic.claude-sonnet-5"
 SYSTEM_PROMPT = """You are the triage agent for ToggleBank's customer support.
 Classify the message as account_question, branch_lookup, schedule_agent or general_question.
 Respond with only JSON: {"query_type": "...", "confidence_score": 0.0}"""
 
+# Used only if LaunchDarkly can't be reached
+FALLBACK = AIAgentConfigDefault(enabled=True, model=ModelConfig(MODEL_ID), instructions=SYSTEM_PROMPT)
 
+
 def route(user_id: str, message: str) -> dict:
-    response = bedrock.converse(
-        modelId=MODEL_ID,
-        system=[{"text": SYSTEM_PROMPT}],
+    context = Context.builder(user_id).build()
+    config = ai_client.agent_config("triage_agent", context, FALLBACK, {"domain": "ToggleBank"})
+    tracker = config.create_tracker()
+
+    response = tracker.track_duration_of(lambda: bedrock.converse(
+        modelId=config.model.name,
+        system=[{"text": config.instructions}],
         messages=[{"role": "user", "content": [{"text": message}]}],
-    )
+    ))
+    usage = response["usage"]
+    tracker.track_tokens(TokenUsage(usage["totalTokens"], usage["inputTokens"], usage["outputTokens"]))
+    tracker.track_success()
+
     text = response["output"]["message"]["content"][0]["text"]
     return json.loads(text.strip().removeprefix("```json").removesuffix("```"))
```

## What each part does

| Change | Lines | What it gives you |
|---|---|---|
| Start the SDK | 3 | One client for the life of the app |
| Keep the old model and prompt as the fallback | 1 | If LaunchDarkly can't be reached, the app behaves exactly as before |
| Fetch the config for this user | 2 | Model and prompt change in LaunchDarkly, with no deploy. Targeting, rollouts and experiments pick the variation per user |
| Track the call | 4 | Duration, tokens and success, which feed cost and latency metrics and experiment results |

## Run it

```sh
pip install boto3 launchdarkly-server-sdk launchdarkly-server-sdk-ai typing_extensions
export LAUNCHDARKLY_SDK_KEY=sdk-...
python -c "from after import route; print(route('user-123', 'Someone used my card without permission'))"
```

`typing_extensions` is listed because `launchdarkly-server-sdk-ai` 1.1.0 imports it without pulling it in.
`Config(...)` connects to production LaunchDarkly. To use another instance, pass `base_uri`, `stream_uri` and `events_uri`.

To compare the files side by side in VS Code: `code --diff before.py after.py`
