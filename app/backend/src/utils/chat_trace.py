"""Per-agent trace for /api/chat callers that ask for it (includeTrace).

CI end-to-end evals use it to see what each agent served and wrote; the UI
doesn't ask for it.
"""

from typing import Any

# Graph nodes in the order they can run, with the AI Config each one evaluates.
AGENT_CONFIG_KEYS = {
    "triage_router": "triage_agent",
    "account_specialist": "account_agent",
    "branch_specialist": "branch_agent",
    "scheduler_specialist": "scheduler_agent",
    "brand_voice": "brand_agent",
}


def build_trace(final_state: dict[str, Any]) -> dict[str, Any]:
    """Build the trace from the workflow's final state.

    `agents` has one entry per agent that ran, in order. `output` is the agent's
    own reply from agent_data, because the final state's messages hold only
    brand voice's. `profile` is the user context brand voice was sent.
    """
    agent_data = final_state.get("agent_data", {})
    agents = []
    for node, config_key in AGENT_CONFIG_KEYS.items():
        data = agent_data.get(node)
        if not data:
            continue
        entry = {
            "agent": node,
            "config_key": config_key,
            "variation": data.get("variation"),
            "model": data.get("model"),
            "served_model": data.get("served_model"),
            "output": data.get("response"),
        }
        if "available_slots" in data:
            entry["available_slots"] = [slot["slot_id"] for slot in data["available_slots"]]
        agents.append(entry)
    return {"agents": agents, "profile": final_state.get("user_context", {})}
