import json
import os

import boto3
import ldclient
from ldai import AIAgentConfigDefault, LDAIClient, ModelConfig
from ldai.tracker import TokenUsage
from ldclient import Context
from ldclient.config import Config

bedrock = boto3.client("bedrock-runtime")
ldclient.set_config(Config(os.environ["LAUNCHDARKLY_SDK_KEY"]))
ai_client = LDAIClient(ldclient.get())

MODEL_ID = "us.anthropic.claude-sonnet-5"
SYSTEM_PROMPT = """You are the triage agent for ToggleBank's customer support.
Classify the message as account_question, branch_lookup, schedule_agent or general_question.
Respond with only JSON: {"query_type": "...", "confidence_score": 0.0}"""

# Used only if LaunchDarkly can't be reached
FALLBACK = AIAgentConfigDefault(enabled=True, model=ModelConfig(MODEL_ID), instructions=SYSTEM_PROMPT)


def route(user_id: str, message: str) -> dict:
    context = Context.builder(user_id).build()
    config = ai_client.agent_config("triage_agent", context, FALLBACK, {"domain": "ToggleBank"})
    tracker = config.create_tracker()

    response = tracker.track_duration_of(lambda: bedrock.converse(
        modelId=config.model.name,
        system=[{"text": config.instructions}],
        messages=[{"role": "user", "content": [{"text": message}]}],
    ))
    usage = response["usage"]
    tracker.track_tokens(TokenUsage(usage["totalTokens"], usage["inputTokens"], usage["outputTokens"]))
    tracker.track_success()

    text = response["output"]["message"]["content"][0]["text"]
    return json.loads(text.strip().removeprefix("```json").removesuffix("```"))
