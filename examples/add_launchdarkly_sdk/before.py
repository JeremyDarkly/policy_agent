import json

import boto3

bedrock = boto3.client("bedrock-runtime")

MODEL_ID = "us.anthropic.claude-sonnet-5"
SYSTEM_PROMPT = """You are the triage agent for ToggleBank's customer support.
Classify the message as account_question, branch_lookup, schedule_agent or general_question.
Respond with only JSON: {"query_type": "...", "confidence_score": 0.0}"""


def route(user_id: str, message: str) -> dict:
    response = bedrock.converse(
        modelId=MODEL_ID,
        system=[{"text": SYSTEM_PROMPT}],
        messages=[{"role": "user", "content": [{"text": message}]}],
    )
    text = response["output"]["message"]["content"][0]["text"]
    return json.loads(text.strip().removeprefix("```json").removesuffix("```"))
