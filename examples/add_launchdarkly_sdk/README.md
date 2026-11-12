# Adding LaunchDarkly to an existing AI agent

A routing agent that classifies customer messages, before and after adding the LaunchDarkly AI SDK
(`launchdarkly-ai-server` 1.0.0). The change adds 26 lines and edits 5.

- [`before.py`](before.py): model and prompt are hardcoded
- [`after.py`](after.py): model and prompt come from the `triage_agent` AI Config, and each call reports its metrics

## The diff

```diff
+import asyncio
 import json
 
 import boto3
+from launchdarkly_ai_server import config, create_handler, parse_template
 
 bedrock = boto3.client("bedrock-runtime")
 
 Respond with only JSON: {"query_type": "...", "confidence_score": 0.0}"""
 
 
-def route(user_id: str, message: str) -> dict:
-    response = bedrock.converse(
-        modelId=MODEL_ID,
-        system=[{"text": SYSTEM_PROMPT}],
+def converse(model_id: str, system_prompt: str, message: str) -> dict:
+    return bedrock.converse(
+        modelId=model_id,
+        system=[{"text": system_prompt}],
         messages=[{"role": "user", "content": [{"text": message}]}],
     )
-    text = response["output"]["message"]["content"][0]["text"]
+
+
+# The existing Bedrock call, as the handler for the config's Bedrock variations
+async def call_bedrock(ld_config, message, tools, variables, history=None):
+    instructions = parse_template(ld_config["instructions"], variables)
+    response = await asyncio.to_thread(converse, ld_config["model"]["name"], instructions, message)
+    return {"output": response["output"]["message"]["content"][0]["text"], "usage": response["usage"]}
+
+
+triage = config(key="triage_agent", handler=create_handler(("Bedrock", "agent"), call_bedrock))
+
+
+def route(user_id: str, message: str) -> dict:
+    try:
+        result = asyncio.run(triage.invoke(message, {"kind": "user", "key": user_id}, {"domain": "ToggleBank"}))
+        text = result.response
+    except Exception:
+        # LaunchDarkly can't be reached or the config is off: behave exactly as before
+        response = converse(MODEL_ID, SYSTEM_PROMPT, message)
+        text = response["output"]["message"]["content"][0]["text"]
     return json.loads(text.strip().removeprefix("```json").removesuffix("```"))
```

## What each part does

| Change | Lines | What it gives you |
|---|---|---|
| Move the Bedrock call into `converse()` | 5 edited | The handler and the fallback both call it |
| Wrap `converse()` as a handler | 4 | The SDK passes it the served model and instructions, and records duration, tokens and success for every call |
| Fill the instructions' variables | 1 | `{{domain}}` in the config's instructions becomes `ToggleBank` |
| Bind the config to the handler | 1 | Model and prompt change in LaunchDarkly, with no deploy. Targeting, rollouts and experiments pick the variation per user |
| Call `invoke()`, and fall back to the old call | 7 | If LaunchDarkly can't be reached or the config is off, `invoke()` raises and the app behaves exactly as before |

There's no client to start and no tracker to call: the SDK connects on the first `invoke()`, reading `LD_SDK_KEY`
from the environment. The handler is registered for the config's `Bedrock` provider in agent mode. A variation that
serves another provider needs that provider's handler package, such as `launchdarkly-ai-openai-messages`.

## Run it

```sh
pip install boto3 launchdarkly-server-sdk launchdarkly-ai-server==1.0.0
export LD_SDK_KEY=sdk-...
python -c "from after import route; print(route('user-123', 'Someone used my card without permission'))"
```

The SDK connects to production LaunchDarkly. To use another instance, set `LD_BASE_URI`, `LD_STREAM_URI` and
`LD_EVENTS_URI`.

To compare the files side by side in VS Code: `code --diff before.py after.py`
