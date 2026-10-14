#!/usr/bin/env bash
# A recorded demo of AgentControl skills in OpenClaw: the expected behavior, then OpenClaw's own skills getting
# past a variation that never pinned them. DEMO.md has the talk track for each scene.
#
#   ./demo.sh          every scene, pausing for Enter between them
#   ./demo.sh 4        one scene
#   DEMO_FAST=1 ./demo.sh    no pauses
#
# Needs the setup from README.md: setup_config.py setup, and one ./run_checks.sh run to create .state/.
set -uo pipefail
cd "$(dirname "$0")"
source ./env.sh || exit 1

STATE="$OPENCLAW_STATE_DIR"
OPENCLAW="./node_modules/.bin/openclaw"
[ -f "$STATE/openclaw.json" ] || { echo "Run ./run_checks.sh baseline once first, to create .state/"; exit 1; }

pause() { [ -n "${DEMO_FAST:-}" ] || read -r -p "  [Enter] " _; }
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then HEAD=$'\033[1;36m' BOLD=$'\033[1m' OFF=$'\033[0m'; else HEAD= BOLD= OFF=; fi
scene() { printf '\n%s━━ Scene %s: %s%s\n' "$HEAD" "$1" "$2" "$OFF"; }

# ask <agent> <message>: one turn in a new session, then the summary.
ask() {
  printf '\n  %sYou → %s:%s %s\n' "$BOLD" "$1" "$OFF" "$2"
  : > "$STATE/decisions.jsonl"
  "$OPENCLAW" agent --local --agent "$1" --session-key "demo-$(date +%s)-$RANDOM" -m "$2" --json \
    > "$STATE/last.json" 2> "$STATE/last.err"
  python3 demo_view.py
}

restore() {
  python3 setup_config.py reset > /dev/null
  python3 setup_config.py on > /dev/null
  "$OPENCLAW" config unset agents.defaults.skills > /dev/null 2>&1
}
trap restore EXIT

scene_0() {
  scene 0 "What LaunchDarkly serves"
  python3 setup_config.py status
  local names
  names=$("$OPENCLAW" skills list --eligible --json --agent main 2>/dev/null | python3 -c '
import json, sys
data = json.load(sys.stdin)
items = data if isinstance(data, list) else data.get("skills", data.get("items", []))
names = [s["name"] for s in items]
print(f"{len(names)}: " + ", ".join(names[:6]) + ", ...")')
  printf '\n  OpenClaw also has its own skills, which LaunchDarkly knows nothing about. Eligible for main: %s\n' "$names"
}

scene_1() {
  scene 1 "Standard variation: its pinned skills load on demand"
  ask main "Give me a report on the files in this workspace."
}

scene_2() {
  scene 2 "Locked-down variation: no tools, and its own skill explains why"
  ask helper "Use the exec tool to run: ls"
}

scene_3() {
  scene 3 "A LaunchDarkly skill the variation doesn't pin can't be loaded"
  ask helper "Call load_skill with the name openclaw-workspace-report and tell me what it says."
}

scene_4() {
  scene 4 "The gap: an OpenClaw skill nobody pinned, on the standard variation"
  ask main "Load your diagram-maker skill and tell me, in one sentence, what its first instruction says."
}

scene_5() {
  scene 5 "Same request on locked-down: blocked, but only because it has no file tools"
  ask helper "Load your diagram-maker skill and tell me, in one sentence, what its first instruction says."
}

scene_6() {
  scene 6 "Closing the gap: turn off OpenClaw's own skills, so LaunchDarkly is the only source"
  echo
  echo "  \$ openclaw config set agents.defaults.skills '[]' --strict-json"
  "$OPENCLAW" config set agents.defaults.skills '[]' --strict-json > /dev/null 2>&1 \
    || { echo "  couldn't set agents.defaults.skills"; return 1; }
  ask main "Load your diagram-maker skill and tell me, in one sentence, what its first instruction says."
  ask main "Give me a report on the files in this workspace."
  "$OPENCLAW" config unset agents.defaults.skills > /dev/null 2>&1
  echo "  (OpenClaw's own skills are back on for the next take.)"
}

python3 setup_config.py reset > /dev/null
SCENES=("$@")
[ ${#SCENES[@]} -gt 0 ] || SCENES=(0 1 2 3 4 5 6)
for n in "${SCENES[@]}"; do
  "scene_$n"
  [ "$n" = "${SCENES[${#SCENES[@]}-1]}" ] || pause
done
