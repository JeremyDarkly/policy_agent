#!/usr/bin/env bash
# Sends traffic through the plugin for the openclaw-agentcontrol-tracing guardrails: N runs each of main and helper,
# alternating, with a tool call in half of them. Run run_checks.sh once first, so .state/ is set up.
#
#   ./trace_load.sh [runs per agent, default 10]
#
# Each run is a real model call (gpt-5.4-mini for main, gpt-5.4-nano for helper). The tracker's duration and error
# events reach openclaw_agent whether or not a run is traced, so guardrails compare traced runs with untraced ones.
set -uo pipefail
cd "$(dirname "$0")"
source ./env.sh || exit 1

RUNS="${1:-10}"
OPENCLAW="./node_modules/.bin/openclaw"
[ -f "$OPENCLAW_CONFIG_PATH" ] || { echo "no OpenClaw state; run ./run_checks.sh first"; exit 1; }

FAILED=0
for i in $(seq 1 "$RUNS"); do
  for agent in main helper; do
    if [ $((i % 2)) = 0 ]; then message="Use the exec tool to run: ls"; else message="Say hello in five words."; fi
    if "$OPENCLAW" agent --local --agent "$agent" --session-key "load-$(date +%s)-$RANDOM" -m "$message" --json \
      > /dev/null 2>&1; then
      echo "  run $i $agent: ok"
    else
      echo "  run $i $agent: failed"
      FAILED=$((FAILED + 1))
    fi
  done
done
echo "$((RUNS * 2)) runs, $FAILED failed"
