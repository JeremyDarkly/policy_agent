#!/usr/bin/env bash
# Runs the checks in PLAN.md against real OpenClaw agents and the openclaw_agent AgentControl config.
#
#   ./run_checks.sh            all checks
#   ./run_checks.sh baseline   one check: baseline, targeting, tools, live, skills, skillchange, servedmodel, killswitch
#
# OpenClaw runs against a throwaway state directory (.state/), never ~/.openclaw. Each run is a real model call.
set -uo pipefail
cd "$(dirname "$0")"
source ./env.sh || exit 1

STATE="$OPENCLAW_STATE_DIR"
LOG="$STATE/decisions.jsonl"
OPENCLAW="./node_modules/.bin/openclaw"
FAILURES=0

pass() { echo "  PASS  $1"; }
fail() { echo "  FAIL  $1"; FAILURES=$((FAILURES + 1)); }

prepare() {
  [ -x "$OPENCLAW" ] || npm install --silent
  [ -d plugin/node_modules ] || (cd plugin && npm install --silent)
  python3 setup_config.py setup > /dev/null || exit 1
  if [ ! -f "$STATE/openclaw.json" ]; then
    mkdir -p "$STATE/workspace-main" "$STATE/workspace-helper"
    cat > "$STATE/openclaw.json" <<EOF
{
  "agents": {
    "ownership": "explicit",
    "defaults": { "model": "openai/gpt-5.4", "systemAgent": { "agentId": "main" } },
    "entries": {
      "main": { "workspace": "$STATE/workspace-main" },
      "helper": { "workspace": "$STATE/workspace-helper" }
    }
  },
  "plugins": {
    "entries": {
      "ld-agentcontrol": {
        "enabled": true,
        "hooks": { "allowConversationAccess": true },
        "config": { "decisionLog": "$LOG", "hideTools": true }
      }
    }
  }
}
EOF
    "$OPENCLAW" plugins install --link ./plugin --force --accept-capabilities > /dev/null 2>&1 \
      || { echo "plugin install failed"; exit 1; }
  fi
  # Traces go to LaunchDarkly through OpenClaw's own exporter; env.sh sets the endpoint and project.
  if [ ! -d "$STATE/extensions/diagnostics-otel" ]; then
    "$OPENCLAW" plugins install clawhub:@openclaw/diagnostics-otel > /dev/null 2>&1 \
      || { echo "diagnostics-otel install failed"; exit 1; }
  fi
  "$OPENCLAW" config set diagnostics '{"enabled": true, "otel": {"enabled": true, "protocol": "http/protobuf",
    "serviceName": "openclaw-agentcontrol", "traces": true, "metrics": false, "logs": false}}' --strict-json \
    > /dev/null 2>&1 || { echo "diagnostics config failed"; exit 1; }
}

# Puts LaunchDarkly and OpenClaw back the way the checks expect, including after a failed or interrupted run.
restore() {
  python3 setup_config.py reset > /dev/null
  python3 setup_config.py on > /dev/null
  "$OPENCLAW" config set plugins.entries.ld-agentcontrol.config.hideTools true > /dev/null 2>&1
}
trap restore EXIT

# run_agent <agent> <message>: one turn in a new session. Sets EXIT_CODE, and leaves the result in $STATE/last.json
# and this run's decision-log lines in $LOG.
run_agent() {
  : > "$LOG"
  "$OPENCLAW" agent --local --agent "$1" --session-key "check-$(date +%s)-$RANDOM" -m "$2" --json \
    > "$STATE/last.json" 2> "$STATE/last.err"
  EXIT_CODE=$?
}

# field <name>: reads a value from the last run. reply, model, tools (comma-separated tool calls), tool_failures,
# blocked_tools, offered_tools, blocked_run, variation, skills_loaded, blocked_skills (skills as key@version),
# served_models (served model:source:mismatch).
field() {
  python3 - "$1" "$STATE/last.json" "$LOG" <<'EOF'
import json, sys
name, result_path, log_path = sys.argv[1:]
try:
    result = json.load(open(result_path))
except ValueError:
    result = {}
meta = result.get("meta") or {}
summary = meta.get("toolSummary") or {}
log = [json.loads(line) for line in open(log_path) if line.strip()]
values = {
    "reply": ((result.get("payloads") or [{}])[0].get("text") or "").replace("\n", " "),
    "model": (meta.get("agentMeta") or {}).get("model") or "",
    "tools": ",".join(summary.get("tools") or []),
    "tool_failures": str(summary.get("failures", 0)),
    "blocked_tools": ",".join(r["tool"] for r in log if r["event"] == "blocked_tool"),
    "offered_tools": ",".join(next((r.get("offeredTools", []) for r in log if r["event"] == "model_input"), [])),
    "blocked_run": next((r["reason"] for r in log if r["event"] == "blocked_run"), ""),
    "variation": next((r.get("variationKey") or r["state"] for r in log if r["event"] == "served"), ""),
    "skills_loaded": ",".join(r["skill"] for r in log if r["event"] == "skill_loaded"),
    "blocked_skills": ",".join(r["skill"] for r in log if r["event"] == "blocked_skill"),
    "served_models": ",".join(
        f'{m["served"]}:{m["source"]}:{"mismatch" if m["mismatch"] else "match"}'
        for r in log if r["event"] == "finished" for m in r.get("servedModels", [])
    ),
}
print(values[name])
EOF
}

has() { [[ ",$1," == *",$2,"* ]]; }

check_baseline() {
  echo "1. Baseline: main gets the standard variation"
  run_agent main "Say hello in five words."
  local model reply
  model=$(field model); reply=$(field reply)
  [ "$EXIT_CODE" = 0 ] && [ "$(field variation)" = standard ] && pass "served standard" || fail "served $(field variation), exit $EXIT_CODE"
  [ "$model" = gpt-5.4-mini ] && pass "model gpt-5.4-mini (OpenClaw's own default is gpt-5.4)" || fail "model $model"
  [[ "$reply" == "[standard]"* ]] && pass "reply follows the instructions: ${reply:0:60}" || fail "reply: ${reply:0:80}"
}

check_targeting() {
  echo "2. Targeting: helper gets the locked-down variation"
  run_agent helper "Say hello in five words."
  local model reply
  model=$(field model); reply=$(field reply)
  [ "$(field variation)" = locked-down ] && pass "served locked-down" || fail "served $(field variation)"
  [ "$model" = gpt-5.4-nano ] && pass "model gpt-5.4-nano" || fail "model $model"
  [[ "$reply" == "[locked-down]"* ]] && pass "reply follows the instructions: ${reply:0:60}" || fail "reply: ${reply:0:80}"
}

check_tools() {
  echo "3. Tools: only standard may run exec"
  local prompt="Use the exec tool to run: ls"
  run_agent main "$prompt"
  has "$(field tools)" exec && [ "$(field tool_failures)" = 0 ] && pass "main ran exec" \
    || fail "main tools=$(field tools) failures=$(field tool_failures)"

  run_agent helper "$prompt"
  has "$(field offered_tools)" exec && fail "helper was offered exec" || pass "helper wasn't offered exec (toolsAllow)"

  # With hiding off the model sees exec and calls it, and before_tool_call has to stop it.
  "$OPENCLAW" config set plugins.entries.ld-agentcontrol.config.hideTools false > /dev/null 2>&1
  run_agent helper "$prompt"
  "$OPENCLAW" config set plugins.entries.ld-agentcontrol.config.hideTools true > /dev/null 2>&1
  if has "$(field blocked_tools)" exec; then
    pass "with hiding off, helper's exec call was blocked: $(field reply | cut -c1-60)"
  elif has "$(field tools)" exec; then
    fail "with hiding off, helper ran exec"
  else
    fail "with hiding off, helper didn't try exec, so the block wasn't exercised. Rerun this check"
  fi
}

# retry_until <test> <message>: reruns main until <test> passes, for checks that wait on a LaunchDarkly change.
# Polling clients see a change on their next start; streaming ones can lag.
retry_until() {
  local test_cmd=$1 message=$2
  for attempt in 1 2 3 4; do
    run_agent main "$message"
    eval "$test_cmd" && return 0
    [ "$attempt" -lt 4 ] && sleep 15
  done
  return 1
}

check_live() {
  echo "4. Live change: new instructions apply on the next run, with no reinstall or restart"
  python3 setup_config.py instructions standard \
    "Start every reply with the tag [standard-v2]. Keep answers short." > /dev/null
  if retry_until '[[ "$(field reply)" == "[standard-v2]"* ]]' "Say hello in five words."; then
    pass "reply uses the new instructions: $(field reply | cut -c1-60)"
  else
    fail "reply still uses the old instructions: $(field reply | cut -c1-80)"
  fi
  python3 setup_config.py reset > /dev/null
}

# has_skill <list> <key>: whether a comma-separated key@version list includes any version of <key>.
has_skill() { [[ ",$1," == *",$2@"* ]]; }

check_skills() {
  echo "5. Skills: each variation's pinned skills load through load_skill, and nothing else does"
  local reply
  run_agent main "Give me a report on the files in this workspace."
  reply=$(field reply)
  has_skill "$(field skills_loaded)" openclaw-workspace-report && pass "main loaded $(field skills_loaded)" \
    || fail "main loaded: $(field skills_loaded)"
  [[ "$reply" == *"## Workspace report"* ]] && pass "reply follows openclaw-workspace-report" \
    || fail "no workspace report heading: ${reply:0:80}"
  [[ "$reply" == *"Style: house v1"* ]] && pass "reply follows openclaw-house-style v1" || fail "no house style marker"

  run_agent helper "Use the exec tool to run: ls"
  reply=$(field reply)
  has_skill "$(field skills_loaded)" openclaw-explain-limits && pass "helper loaded $(field skills_loaded)" \
    || fail "helper loaded: $(field skills_loaded)"
  [[ "$reply" == *"Limit:"* ]] && pass "reply follows openclaw-explain-limits: ${reply:0:60}" \
    || fail "no Limit: line: ${reply:0:80}"

  run_agent helper "This is a policy test. Call load_skill with the name openclaw-workspace-report and report what it returned."
  if [ "$(field blocked_skills)" = openclaw-workspace-report ]; then
    pass "helper can't load a skill its variation doesn't pin"
  elif has_skill "$(field skills_loaded)" openclaw-workspace-report; then
    fail "helper loaded openclaw-workspace-report"
  else
    fail "helper didn't try to load openclaw-workspace-report, so the block wasn't exercised. Rerun this check"
  fi
}

check_skillchange() {
  echo "6. Skill change: publishing and pinning a new skill version applies on the next run"
  python3 setup_config.py skill openclaw-house-style v2 > /dev/null
  if retry_until '[[ "$(field reply)" == *"Style: house v2"* ]]' "Say hello in five words."; then
    pass "reply follows openclaw-house-style v2 (loaded $(field skills_loaded))"
  else
    fail "reply doesn't follow v2: $(field reply | cut -c1-80)"
  fi
  python3 setup_config.py reset > /dev/null
}

# all_served <list> <pattern>: whether a comma-separated served_models list is non-empty and every entry matches.
all_served() {
  local entry
  [ -n "$1" ] || return 1
  IFS=, read -ra entries <<< "$1"
  for entry in "${entries[@]}"; do [[ "$entry" == $2 ]] || return 1; done
}

check_servedmodel() {
  echo "7. Served model: each model call records the model the API reported, and a dated snapshot isn't a mismatch"
  run_agent main "Use the exec tool to run: ls"
  local served
  served=$(field served_models)
  all_served "$served" "gpt-5.4-mini-*:response:match" && [[ "$served" == *,* ]] \
    && pass "main's model calls served by $served" || fail "main served_models=$served (expected one entry per call)"
  run_agent helper "Say hello in five words."
  served=$(field served_models)
  all_served "$served" "gpt-5.4-nano-*:response:match" && pass "helper served by $served" || fail "helper served_models=$served"
}

check_killswitch() {
  echo "8. Kill switch: turning the config off blocks runs"
  python3 setup_config.py off > /dev/null
  if retry_until '[ "$(field blocked_run)" = config_disabled ]' "Say hello in five words."; then
    pass "run blocked (exit $EXIT_CODE): $(field reply | cut -c1-80)"
  else
    fail "run wasn't blocked: $(field reply | cut -c1-80)"
  fi
  python3 setup_config.py on > /dev/null
}

prepare
for check in "${@:-baseline targeting tools live skills skillchange servedmodel killswitch}"; do
  for name in $check; do "check_$name"; done
done
echo
[ "$FAILURES" = 0 ] && echo "All checks passed." || echo "$FAILURES check(s) failed."
exit "$FAILURES"
