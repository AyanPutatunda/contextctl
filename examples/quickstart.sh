#!/usr/bin/env bash
# End-to-end walkthrough of the four layers on synthetic notes.
# Usage: bash examples/quickstart.sh [bundle-dir]
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
B="${1:-$(mktemp -d)/bundle}"
export CONTEXTCTL_BUNDLE="$B" CONTEXTCTL_REVIEWER="demo-reviewer"

echo "== 1. Generation (untrusted): init + notes connector"
contextctl init
NOTES="$(mktemp -d)"; cp "$HERE"/notes/*.md "$NOTES"/
contextctl ingest "$NOTES"

echo "== 2. Curation (trust boundary): validate + reconcile, review, merge"
contextctl curate
for id in $(contextctl review list | python3 -c 'import json,sys;[print(r["id"]) for r in json.load(sys.stdin)]'); do
  contextctl review approve "$id"
done
contextctl merge -m "admit synthetic notes"

echo "== Owner changes upstream -> Conflict held for a human"
cp "$HERE/notes/payments-pipeline-v2.md.later" "$NOTES/payments-pipeline.md"
contextctl ingest "$NOTES"
contextctl curate
ID=$(contextctl review list | python3 -c 'import json,sys;[print(r["id"]) for r in json.load(sys.stdin) if r["reconciliation"]["decision"]=="Conflict"]')
contextctl review approve "$ID" --resolve '{"owner": "blair"}'
contextctl merge -m "resolve owner conflict"

echo "== 3. Metadata (derived): SCD2 history; delete + rebuild check"
P=concepts/notes/payments-pipeline-owner.md
contextctl history "$P" --field owner
contextctl sidecar check

echo "== 4. Serving: orient, search, read, as-of read, provenance"
contextctl orient --limit 5
contextctl search "payments owner"
contextctl read "$P" --body-only
FIRST=$(git -C "$B" rev-list --first-parent main | sed -n 2p)
echo "-- as of $FIRST (before the conflict was resolved):"
contextctl read "$P" --as-of "$FIRST" | grep '^owner'
contextctl provenance "$P"
echo "bundle at $B"
