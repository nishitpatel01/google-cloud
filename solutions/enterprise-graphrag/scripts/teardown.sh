#!/usr/bin/env bash
# Safe teardown script for ACC-05 Cloud Run deployment resources.
# Preserves BigQuery dataset and GCS upload bucket unless --purge-data is explicitly passed.
#
# Usage:
#   ./scripts/teardown.sh --dry-run
#   ./scripts/teardown.sh --yes
#   ./scripts/teardown.sh --yes --purge-data
set -euo pipefail

readonly SOLUTION_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${SOLUTION_ROOT}/deploy/cloudrun.env"
MODE=""
PURGE_DATA="false"
declare -A CFG=()

die() { echo "ERROR: $*" >&2; exit 1; }

while (($#)); do
  case "$1" in
    --env-file) (($# >= 2)) || die "--env-file requires a path"; ENV_FILE="$2"; shift 2 ;;
    --dry-run) MODE="dry-run"; shift ;;
    --yes) MODE="apply"; shift ;;
    --purge-data) PURGE_DATA="true"; shift ;;
    -h|--help)
      echo "Usage: ./scripts/teardown.sh [--env-file PATH] [--purge-data] (--dry-run | --yes)"
      exit 0
      ;;
    *) die "Unknown argument: $1" ;;
  esac
done
[[ -n "$MODE" ]] || die "Pass --dry-run to preview or --yes to tear down."
[[ -f "$ENV_FILE" ]] || die "Config file not found: $ENV_FILE"

while IFS= read -r line || [[ -n "$line" ]]; do
  line="${line%$'\r'}"
  [[ -z "${line//[[:space:]]/}" || "$line" =~ ^[[:space:]]*# ]] && continue
  k="${line%%=*}"; k="${k//[[:space:]]/}"
  v="${line#*=}"; v="${v#\"}"; v="${v%\"}"; v="${v#\'}"; v="${v%\'}"
  CFG["$k"]="$v"
done < "$ENV_FILE"

PROJ="${CFG[GCP_PROJECT_ID]:-}"
REGION="${CFG[GCP_REGION]:-us-central1}"
SERVICE_NAME="${CFG[SERVICE_NAME]:-acc05-graphrag-ui}"
REPLAY_SERVICE_NAME="${CFG[REPLAY_SERVICE_NAME]:-acc05-graphrag-replay}"
ARTIFACT_REPO="${CFG[ARTIFACT_REPO]:-acc05-graphrag}"
RUNTIME_SA="${CFG[RUNTIME_SA_NAME]:-acc05-graphrag-run}@${PROJ}.iam.gserviceaccount.com"
REPLAY_SA="${CFG[REPLAY_SA_NAME]:-acc05-graphrag-replay}@${PROJ}.iam.gserviceaccount.com"
BQ_DATASET="${CFG[BQ_DATASET]:-acc05_enterprise_graphrag}"
GCS_BUCKET="${CFG[GCS_BUCKET]:-${PROJ}-acc05-graphrag-uploads}"

[[ -n "$PROJ" ]] || die "GCP_PROJECT_ID is required in $ENV_FILE"

echo "=== ACC-05 Teardown Plan (project=${PROJ}, region=${REGION}, purge_data=${PURGE_DATA}) ==="
echo "  - Delete Cloud Run service : ${SERVICE_NAME}"
echo "  - Delete Cloud Run service : ${REPLAY_SERVICE_NAME}"
echo "  - Delete Artifact Repo     : ${ARTIFACT_REPO}"
echo "  - Delete Service Accounts  : ${RUNTIME_SA}, ${REPLAY_SA}"
if [[ "$PURGE_DATA" == "true" ]]; then
  echo "  - PURGE BigQuery dataset   : ${PROJ}.${BQ_DATASET}"
  echo "  - PURGE GCS upload bucket  : gs://${GCS_BUCKET}"
else
  echo "  - PRESERVE BigQuery/GCS    : ${PROJ}.${BQ_DATASET}, gs://${GCS_BUCKET} (pass --purge-data to delete)"
fi

if [[ "$MODE" == "dry-run" ]]; then
  echo "DRY RUN: no resources were deleted."
  exit 0
fi

gc() {
  CLOUDSDK_METRICS_ENVIRONMENT="${CLOUDSDK_METRICS_ENVIRONMENT:+$CLOUDSDK_METRICS_ENVIRONMENT }datacloud.cli" \
    gcloud --project="$PROJ" --quiet "$@"
}

gc run services delete "$SERVICE_NAME" --region="$REGION" || true
gc run services delete "$REPLAY_SERVICE_NAME" --region="$REGION" || true
gc artifacts repositories delete "$ARTIFACT_REPO" --location="$REGION" || true
gc iam service-accounts delete "$RUNTIME_SA" || true
gc iam service-accounts delete "$REPLAY_SA" || true

if [[ "$PURGE_DATA" == "true" ]]; then
  CLOUDSDK_METRICS_ENVIRONMENT="datacloud.cli" bq --project_id="$PROJ" rm -r -f -d "${PROJ}:${BQ_DATASET}" || true
  gc storage rm -r "gs://${GCS_BUCKET}" || true
fi

echo "Teardown complete. Run GCP_PROJECT_ID=${PROJ} ./scripts/inventory_resources.sh to audit remaining resources."
