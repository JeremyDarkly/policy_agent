import asyncio
import json

import boto3
from launchdarkly_ai_server import config, create_handler, parse_template

bedrock = boto3.client("bedrock-runtime")

MODEL_ID = "us.anthropic.claude-sonnet-5"
SYSTEM_PROMPT = """You are the triage agent for ToggleBank's customer support.
Classify the message as account_question, branch_lookup, schedule_agent or general_question.
Respond with only JSON: {"query_type": "...", "confidence_score": 0.0}"""


def converse(model_id: str, system_prompt: str, message: str) -> dict:
    return bedrock.converse(
        modelId=model_id,
        system=[{"text": system_prompt}],
        messages=[{"role": "user", "content": [{"text": message}]}],
    )


# The existing Bedrock call, as the handler for the config's Bedrock variations
async def call_bedrock(ld_config, message, tools, variables, history=None):
    instructions = parse_template(ld_config["instructions"], variables)
    response = await asyncio.to_thread(converse, ld_config["model"]["name"], instructions, message)
    return {"output": response["output"]["message"]["content"][0]["text"], "usage": response["usage"]}


triage = config(key="triage_agent", handler=create_handler(("Bedrock", "agent"), call_bedrock))


def route(user_id: str, message: str) -> dict:
    try:
        result = asyncio.run(triage.invoke(message, {"kind": "user", "key": user_id}, {"domain": "ToggleBank"}))
        text = result.response
    except Exception:
        # LaunchDarkly can't be reached or the config is off: behave exactly as before
        response = converse(MODEL_ID, SYSTEM_PROMPT, message)
        text = response["output"]["message"]["content"][0]["text"]
    return json.loads(text.strip().removeprefix("```json").removesuffix("```"))
