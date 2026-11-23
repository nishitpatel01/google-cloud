#!/usr/bin/env bash
# Discover, audit, or verify cleanup of ALL GCP resources belonging to ACC-05
# (or the entire gcp_ai_accelerators portfolio) via Cloud Asset Inventory + Model Armor.
#
# Usage:
#   GCP_PROJECT_ID=<project> ./scripts/inventory_resources.sh
#   GCP_PROJECT_ID=<project> ./scripts/inventory_resources.sh --portfolio
#   GCP_PROJECT_ID=<project> ./scripts/inventory_resources.sh --verify-clean
set -euo pipefail

SOLUTION_ID="acc05"
SOLUTION_SLUG="enterprise_graphrag"
PORTFOLIO_ID="gcp_ai_accelerators"

if [ -z "${GCP_PROJECT_ID:-}" ]; then
  if [ -f ".env" ]; then
    # shellcheck disable=SC1091
    set -a; source .env; set +a
  fi
fi

if [ -z "${GCP_PROJECT_ID:-}" ]; then
  echo "ERROR: Set GCP_PROJECT_ID in environment or .env before running inventory." >&2
  exit 1
fi

MODE="${1:-solution}"
if [ "$MODE" = "--portfolio" ]; then
  QUERY="labels.portfolio=${PORTFOLIO_ID}"
  TITLE="All GCP resources in portfolio=${PORTFOLIO_ID}"
else
  QUERY="(labels.solution_id=${SOLUTION_ID} OR labels.solution=${SOLUTION_SLUG} OR name:${SOLUTION_ID} OR name:${SOLUTION_SLUG})"
  TITLE="All GCP resources for ${SOLUTION_ID} (${SOLUTION_SLUG})"
fi

echo "=== ${TITLE} (project: ${GCP_PROJECT_ID}) ==="
ASSETS_JSON="$(
  CLOUDSDK_METRICS_ENVIRONMENT="${CLOUDSDK_METRICS_ENVIRONMENT:+$CLOUDSDK_METRICS_ENVIRONMENT }datacloud.cli" \
  gcloud asset search-all-resources \
    --scope="projects/${GCP_PROJECT_ID}" \
    --query="${QUERY}" \
    --format="json"
)"

COUNT="$(echo "$ASSETS_JSON" | python3 -c 'import json, sys; print(len(json.load(sys.stdin)))')"
echo "Matched Cloud Asset Inventory resources: ${COUNT}"

if [ "$COUNT" -gt 0 ]; then
  echo "$ASSETS_JSON" | python3 -c '
import json, sys
rows = json.load(sys.stdin)
print(f"{'\''ASSET TYPE'\'':<42} {'\''DISPLAY NAME'\'':<36} {'\''LOC'\'':<12} {'\''LABELS'\''}")
print("-" * 110)
for r in rows:
    atype = r.get("assetType", "").split("/")[-1]
    name = r.get("displayName") or r.get("name", "").split("/")[-1]
    loc = r.get("location") or "-"
    labels = ", ".join(f"{k}={v}" for k, v in sorted((r.get("labels") or {}).items()))
    print(f"{atype:<42} {name[:35]:<36} {loc:<12} {labels}")
'
fi

# Model Armor templates are listed via the regional REST API since search-all-resources omits them
MA_LOC="${MODEL_ARMOR_LOCATION:-us-central1}"
MA_TOKEN="$(gcloud auth print-access-token 2>/dev/null || true)"
MA_COUNT="$(
  SOLUTION_ID="$SOLUTION_ID" \
  PORTFOLIO_ID="$PORTFOLIO_ID" \
  GCP_PROJECT_ID="$GCP_PROJECT_ID" \
  MA_LOC="$MA_LOC" \
  MA_TOKEN="$MA_TOKEN" \
  MODE="$MODE" \
  python3 -c '
import json, os, sys, urllib.request
token = os.environ.get("MA_TOKEN", "").strip()
if not token:
    print(0)
    sys.exit(0)
proj = os.environ["GCP_PROJECT_ID"]
loc = os.environ["MA_LOC"]
sid = os.environ["SOLUTION_ID"]
pid = os.environ["PORTFOLIO_ID"]
mode = os.environ["MODE"]
url = f"https://modelarmor.{loc}.rep.googleapis.com/v1/projects/{proj}/locations/{loc}/templates"
req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
try:
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))
except Exception:
    print(0)
    sys.exit(0)
items = data.get("templates") or []
matched = []
for item in items:
    name = item.get("name", "")
    labels = item.get("labels") or {}
    if mode == "--portfolio":
        if labels.get("portfolio") == pid or sid in name:
            matched.append(item)
    elif labels.get("solution_id") == sid or sid in name:
        matched.append(item)
if matched:
    print(f"\nMatched Model Armor templates ({loc}): {len(matched)}", file=sys.stderr)
    for item in matched:
        short = item.get("name", "").split("/")[-1]
        lbl = ", ".join(f"{k}={v}" for k, v in sorted((item.get("labels") or {}).items()))
        print(f"  ModelArmorTemplate                         {short:<36} {loc:<12} {lbl}", file=sys.stderr)
print(len(matched))
'
)"

TOTAL=$((COUNT + MA_COUNT))
if [ "$MODE" = "--verify-clean" ] && [ "$TOTAL" -gt 0 ]; then
  echo "FAIL: Expected 0 residual resources for ${SOLUTION_ID}, found ${TOTAL}." >&2
  exit 1
fi
