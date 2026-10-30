#!/usr/bin/env bash
# Creates or updates the IdeaFactory pipeline labels. Safe to rerun.
# Usage: scripts/sync-labels.sh [owner/repo]   (defaults to the current repo)
set -euo pipefail

REPO="${1:-$(gh repo view --json nameWithOwner -q .nameWithOwner)}"

label() {
  local name="$1" color="$2" desc="$3"
  gh label create "$name" --repo "$REPO" --color "$color" --description "$desc" --force >/dev/null
  echo "  $name"
}

echo "Lifecycle labels in $REPO"
label idea       "C5DEF5" "Filed, no proposal yet"
label proposal   "0E8A16" "Proposal written, waiting for review"
label approved   "1D76DB" "Approved; build may start"
label building   "FBCA04" "Build run is live or paused"
label in-review  "5319E7" "PR open, waiting for review and Autopilot approve"
label done       "0E8A16" "Release plan completed in staging"
label declined   "E4E669" "Parked; closed after 7 days"

echo "Modifier labels"
label spike        "D93F0B" "Proposal run may execute code and probe staging"
label blocked      "B60205" "Run stopped on a question; remove to resume"
label product-gap  "F9D0C4" "A Gap: report, not an idea"

echo "Product areas"
label agent-control "BFD4F2" "Product area"
label observability "BFD4F2" "Product area"
label evals         "BFD4F2" "Product area"
label autopilot     "BFD4F2" "Product area"
label sdk           "BFD4F2" "Product area"

echo "Done."
