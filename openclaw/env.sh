# Sourced by run_checks.sh. Exports only the variables the test needs, from the repo's env file, and points OpenClaw
# at a throwaway state directory so an existing ~/.openclaw is never touched.
OPENCLAW_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$OPENCLAW_DIR/../${ENV_FILE_NAME:-.env.staging-org}"
[ -f "$ENV_FILE" ] || { echo "env file not found: $ENV_FILE" >&2; return 1; }
for name in LAUNCHDARKLY_SDK_KEY LAUNCHDARKLY_BASE_URI LAUNCHDARKLY_STREAM_URI LAUNCHDARKLY_EVENTS_URI OPENAI_API_KEY; do
  value="$(grep -E "^${name}=" "$ENV_FILE" | tail -1 | cut -d= -f2- | sed -e "s/^[\"']//" -e "s/[\"']$//")"
  [ -n "$value" ] && export "$name=$value"
done
export OPENCLAW_STATE_DIR="$OPENCLAW_DIR/.state"
export OPENCLAW_CONFIG_PATH="$OPENCLAW_STATE_DIR/openclaw.json"
