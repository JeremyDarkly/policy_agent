#!/bin/bash
# Install (or update) the IdeaFactory watcher as a launchd LaunchAgent.
#
# Copies watcher.py and prompts/ to $IDEAFACTORY_HOME/bin so the job does not
# depend on which branch the checkout is on, then loads a LaunchAgent that
# runs `watcher.py poll` every $INTERVAL seconds while you are logged in.
#
#   .claude/pipeline/watcher/install.sh             install or update
#   .claude/pipeline/watcher/install.sh uninstall   unload and remove the job
#
# Extra watcher settings (IDEAFACTORY_PROPOSALS, IDEAFACTORY_BUDGET_USD, ...)
# can be exported before running this; they are written into the plist.
set -euo pipefail

LABEL=com.ideafactory.watcher
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
IDEAFACTORY_HOME="${IDEAFACTORY_HOME:-$HOME/.ideafactory}"
INTERVAL="${INTERVAL:-300}"
SRC="$(cd "$(dirname "$0")" && pwd)"
CHECKOUT="$(dirname "$(git -C "$SRC" rev-parse --path-format=absolute --git-common-dir)")"

if [[ "${1:-}" == "uninstall" ]]; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  echo "Removed $LABEL. State and logs are still in $IDEAFACTORY_HOME."
  exit 0
fi

mkdir -p "$IDEAFACTORY_HOME/bin" "$HOME/Library/LaunchAgents"
rm -rf "$IDEAFACTORY_HOME/bin/prompts"
cp "$SRC/watcher.py" "$IDEAFACTORY_HOME/bin/"
cp -R "$SRC/prompts" "$IDEAFACTORY_HOME/bin/"

env_entries=""
for name in IDEAFACTORY_REPO IDEAFACTORY_GH_USER IDEAFACTORY_BASE IDEAFACTORY_PROPOSALS \
            IDEAFACTORY_MAX_LIVE IDEAFACTORY_BUDGET_USD IDEAFACTORY_WALL_HOURS \
            IDEAFACTORY_HEARTBEAT_HOURS IDEAFACTORY_PERMISSION_MODE IDEAFACTORY_MODEL CLAUDE_BIN; do
  if [[ -n "${!name:-}" ]]; then
    env_entries+="    <key>$name</key><string>${!name}</string>"$'\n'
  fi
done

cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>$IDEAFACTORY_HOME/bin/watcher.py</string>
    <string>poll</string>
  </array>
  <key>StartInterval</key><integer>$INTERVAL</integer>
  <key>RunAtLoad</key><true/>
  <!-- Runs outlive the poll that launched them. -->
  <key>AbandonProcessGroup</key><true/>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>IDEAFACTORY_HOME</key><string>$IDEAFACTORY_HOME</string>
    <key>IDEAFACTORY_CHECKOUT</key><string>$CHECKOUT</string>
$env_entries  </dict>
  <key>StandardOutPath</key><string>$IDEAFACTORY_HOME/launchd.log</string>
  <key>StandardErrorPath</key><string>$IDEAFACTORY_HOME/launchd.log</string>
</dict>
</plist>
PLIST

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Loaded $LABEL: polls every ${INTERVAL}s for checkout $CHECKOUT."
echo "Logs: $IDEAFACTORY_HOME/watcher.log  Runs: python3 $IDEAFACTORY_HOME/bin/watcher.py status"
