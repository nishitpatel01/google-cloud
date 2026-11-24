#!/usr/bin/env bash
# Turnkey 1-command Tier-1 Serverless Cloud Run deployer for ACC-05 (Enterprise GraphRAG).
#
# Usage:
#   cp deploy/cloudrun.env.example deploy/cloudrun.env   # git-ignored
#   ./scripts/deploy.sh --dry-run                        # validate config & print plan (no gcloud calls)
#   ./scripts/deploy.sh --yes                            # provision, build remotely, deploy & verify
#   ./scripts/deploy.sh --yes --skip-seed                # skip BigQuery reseeding if already populated
set -euo pipefail

readonly SOLUTION_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${SOLUTION_ROOT}/deploy/cloudrun.env"
MODE=""
SKIP_SEED="false"
declare -A CFG=()
TMP_FILES=()

readonly PORTFOLIO_ID="gcp_ai_accelerators"
readonly SOLUTION_ID="acc05"
readonly SOLUTION_SLUG="enterprise_graphrag"

readonly TIER1_APIS=(
  "run.googleapis.com"
  "aiplatform.googleapis.com"
  "bigquery.googleapis.com"
  "bigqueryreservation.googleapis.com"
  "discoveryengine.googleapis.com"
  "storage.googleapis.com"
  "modelarmor.googleapis.com"
  "dlp.googleapis.com"
  "artifactregistry.googleapis.com"
  "cloudbuild.googleapis.com"
  "cloudasset.googleapis.com"
  "iap.googleapis.com"
)

readonly RUNTIME_ROLES=(
  "roles/aiplatform.user"
  "roles/bigquery.jobUser"
  "roles/discoveryengine.viewer"
  "roles/modelarmor.user"
  "roles/dlp.user"
  "roles/logging.logWriter"
  "roles/cloudtrace.agent"
)

die() { echo "ERROR: $*" >&2; exit 1; }
plan() { echo "PLAN  $*"; }

usage() {
  cat <<'EOF'
Usage: ./scripts/deploy.sh [--env-file PATH] [--skip-seed] (--dry-run | --yes)
  --env-file PATH   Deployment env file (default: deploy/cloudrun.env)
  --skip-seed       Skip BigQuery dataset seeding (useful on fast UI/code redeployments)
  --dry-run         Validate config and print resource/label plan without calling gcloud
  --yes             Execute idempotent deployment on Google Cloud
EOF
}

parse_args() {
  while (($#)); do
    case "$1" in
      --env-file) (($# >= 2)) || die "--env-file requires a path"; ENV_FILE="$2"; shift 2 ;;
      --skip-seed) SKIP_SEED="true"; shift ;;
      --dry-run) MODE="dry-run"; shift ;;
      --yes) MODE="apply"; shift ;;
      -h|--help) usage; exit 0 ;;
      *) usage >&2; die "Unknown argument: $1" ;;
    esac
  done
  [[ -n "$MODE" ]] || { usage >&2; die "Pass --dry-run to preview or --yes to apply."; }
}

load_env() {
  [[ -f "$ENV_FILE" ]] || die "Config file not found: $ENV_FILE (copy deploy/cloudrun.env.example)"
  local line key val
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%$'\r'}"
    [[ -z "${line//[[:space:]]/}" || "$line" =~ ^[[:space:]]*# ]] && continue
    [[ "$line" == *=* ]] || die "Invalid KEY=VALUE line in $ENV_FILE: $line"
    key="${line%%=*}"; key="${key//[[:space:]]/}"
    val="${line#*=}"; val="${val#\"}"; val="${val%\"}"; val="${val#\'}"; val="${val%\'}"
    CFG["$key"]="$val"
  done < "$ENV_FILE"
}

cfg() { echo "${CFG[$1]:-${2:-}}"; }

apply_defaults() {
  local pair k
  for pair in \
    GCP_REGION=us-central1 APP_ENV=demo BQ_DATASET=acc05_enterprise_graphrag BQ_LOCATION=US \
    ARTIFACT_REPO=acc05-graphrag SERVICE_NAME=acc05-graphrag-ui REPLAY_SERVICE_NAME=acc05-graphrag-replay \
    RUNTIME_SA_NAME=acc05-graphrag-run REPLAY_SA_NAME=acc05-graphrag-replay \
    MODEL_ARMOR_LOCATION=us-central1 MODEL_ARMOR_TEMPLATE_ID=acc05-enterprise-graphrag-guardrail \
    ACCESS_MODE=iap DEPLOY_REPLAY=true ENABLE_CLOUD_DLP=false \
    MIN_INSTANCES=0 MAX_INSTANCES=2 CPU=1 MEMORY=2Gi TIMEOUT=600 CONCURRENCY=20; do
    k="${pair%%=*}"
    [[ -n "${CFG[$k]:-}" ]] || CFG["$k"]="${pair#*=}"
  done
  [[ -n "${CFG[GCS_BUCKET]:-}" || -z "${CFG[GCP_PROJECT_ID]:-}" ]] \
    || CFG[GCS_BUCKET]="${CFG[GCP_PROJECT_ID]}-acc05-graphrag-uploads"
  [[ -n "${CFG[IMAGE_TAG]:-}" ]] || CFG[IMAGE_TAG]="v0.6.0"
}

check_re() { [[ "$(cfg "$1")" =~ $2 ]] || die "$1 is invalid ('$(cfg "$1")'): $3"; }

validate_config() {
  [[ -n "$(cfg GCP_PROJECT_ID)" ]] || die "GCP_PROJECT_ID is required"
  check_re GCP_PROJECT_ID '^[a-z][a-z0-9-]{4,28}[a-z0-9]$' "6-30 lowercase letters, digits, hyphens"
  check_re GCP_REGION '^[a-z]+-[a-z]+[0-9]+$' "valid GCP region (e.g. us-central1)"
  check_re APP_ENV '^[a-z][a-z0-9_-]{1,31}$' "lowercase label-safe env name"
  check_re BQ_DATASET '^[a-z0-9_]{2,40}$' "lowercase dataset name matching ^[a-z0-9_]{2,40}$"
  check_re ARTIFACT_REPO '^[a-z][a-z0-9-]{2,60}$' "lowercase Artifact Registry repository ID"
  check_re SERVICE_NAME '^[a-z][a-z0-9-]{2,48}$' "lowercase Cloud Run service name"
  check_re REPLAY_SERVICE_NAME '^[a-z][a-z0-9-]{2,48}$' "lowercase Cloud Run replay service name"
  check_re RUNTIME_SA_NAME '^[a-z][a-z0-9-]{4,28}[a-z0-9]$' "6-30 char service account ID"
  check_re REPLAY_SA_NAME '^[a-z][a-z0-9-]{4,28}[a-z0-9]$' "6-30 char service account ID"
  check_re GCS_BUCKET '^[a-z0-9][a-z0-9._-]{1,61}[a-z0-9]$' "valid GCS bucket name"
  [[ "$(cfg ACCESS_MODE)" == "iap" || "$(cfg ACCESS_MODE)" == "public" ]] \
    || die "ACCESS_MODE must be 'iap' or 'public'"
  if [[ "$(cfg ACCESS_MODE)" == "iap" && -z "$(cfg IAP_MEMBERS)" ]]; then
    die "IAP_MEMBERS is required when ACCESS_MODE=iap (e.g., user:you@example.com)"
  fi
}

gc() {
  CLOUDSDK_METRICS_ENVIRONMENT="${CLOUDSDK_METRICS_ENVIRONMENT:+$CLOUDSDK_METRICS_ENVIRONMENT }datacloud.cli" \
    gcloud --project="$(cfg GCP_PROJECT_ID)" --quiet "$@"
}

labels_csv() {
  echo "portfolio=${PORTFOLIO_ID},solution_id=${SOLUTION_ID},solution=${SOLUTION_SLUG},tier=tier1_serverless,managed_by=deploy_script,env=$(cfg APP_ENV)"
}

retry_iam() {
  local attempt
  for attempt in 1 2 3 4 5; do
    if "$@" >/dev/null 2>&1; then return 0; fi
    sleep $((attempt * 2))
  done
  "$@"
}

print_plan() {
  local proj region image labels
  proj="$(cfg GCP_PROJECT_ID)"
  region="$(cfg GCP_REGION)"
  image="${region}-docker.pkg.dev/${proj}/$(cfg ARTIFACT_REPO)/enterprise-graphrag:$(cfg IMAGE_TAG)"
  labels="$(labels_csv)"
  echo "=== ACC-05 Risk & Contagion Intelligence · Cloud Run Deployment Plan ==="
  plan "Project / Region : ${proj} / ${region} (env=$(cfg APP_ENV))"
  plan "6-Key GCP Labels : ${labels}"
  plan "Artifact Repo    : $(cfg ARTIFACT_REPO) (DOCKER, ${region})"
  plan "Container Image  : ${image}"
  plan "Runtime SA       : $(cfg RUNTIME_SA_NAME)@${proj}.iam.gserviceaccount.com"
  plan "Upload Bucket    : gs://$(cfg GCS_BUCKET) (UBLA + PAP + 90d lifecycle)"
  plan "BigQuery Dataset : ${proj}.$(cfg BQ_DATASET) ($(cfg BQ_LOCATION), skip_seed=${SKIP_SEED})"
  plan "Model Armor      : $(cfg MODEL_ARMOR_TEMPLATE_ID) ($(cfg MODEL_ARMOR_LOCATION))"
  plan "Live Service     : $(cfg SERVICE_NAME) (ACCESS_MODE=$(cfg ACCESS_MODE), min=$(cfg MIN_INSTANCES), max=$(cfg MAX_INSTANCES))"
  if [[ "$(cfg DEPLOY_REPLAY)" == "true" ]]; then
    plan "Replay Service   : $(cfg REPLAY_SERVICE_NAME) (public zero-credential, role-less SA $(cfg REPLAY_SA_NAME))"
  fi
}

ensure_sa() {
  local sa_id="$1" display="$2"
  local email="${sa_id}@$(cfg GCP_PROJECT_ID).iam.gserviceaccount.com"
  if ! gc iam service-accounts describe "$email" >/dev/null 2>&1; then
    echo "==> Creating service account ${email}"
    gc iam service-accounts create "$sa_id" --display-name="$display" >/dev/null
  fi
}

write_env_yaml() {
  local out="$1"
  shift
  : > "$out"; chmod 600 "$out"
  local kv k v
  for kv in "$@"; do
    k="${kv%%=*}"; v="${kv#*=}"
    printf '%s: "%s"\n' "$k" "${v//\\/\\\\}" >> "$out"
  done
}

apply_deploy() {
  trap 'rm -f "${TMP_FILES[@]:-}"' EXIT
  local proj region labels image live_sa replay_sa project_number
  proj="$(cfg GCP_PROJECT_ID)"
  region="$(cfg GCP_REGION)"
  labels="$(labels_csv)"
  image="${region}-docker.pkg.dev/${proj}/$(cfg ARTIFACT_REPO)/enterprise-graphrag:$(cfg IMAGE_TAG)"
  live_sa="$(cfg RUNTIME_SA_NAME)@${proj}.iam.gserviceaccount.com"
  replay_sa="$(cfg REPLAY_SA_NAME)@${proj}.iam.gserviceaccount.com"

  project_number="$(gc projects describe "$proj" --format='value(projectNumber)')"

  echo "==> [1/8] Enabling required Tier-1 Google Cloud APIs"
  gc services enable "${TIER1_APIS[@]}" >/dev/null

  echo "==> [2/8] Ensuring labeled Artifact Registry repository $(cfg ARTIFACT_REPO)"
  if ! gc artifacts repositories describe "$(cfg ARTIFACT_REPO)" --location="$region" >/dev/null 2>&1; then
    gc artifacts repositories create "$(cfg ARTIFACT_REPO)" \
      --repository-format=docker \
      --location="$region" \
      --description="ACC-05 Risk & Contagion Intelligence container images" \
      --labels="$labels" >/dev/null
  else
    gc artifacts repositories update "$(cfg ARTIFACT_REPO)" \
      --location="$region" --update-labels="$labels" >/dev/null
  fi

  echo "==> [3/8] Building multi-stage container remotely via Cloud Build"
  (cd "$SOLUTION_ROOT" && gc builds submit --tag "$image" .)

  echo "==> [4/8] Ensuring dedicated least-privilege Runtime SA & IAM bindings"
  ensure_sa "$(cfg RUNTIME_SA_NAME)" "ACC-05 Enterprise GraphRAG Runtime"
  local role
  for role in "${RUNTIME_ROLES[@]}"; do
    retry_iam gc projects add-iam-policy-binding "$proj" \
      --member="serviceAccount:${live_sa}" --role="$role" --condition=None
  done

  echo "==> [5/8] Ensuring private labeled GCS upload bucket gs://$(cfg GCS_BUCKET)"
  if ! gc storage buckets describe "gs://$(cfg GCS_BUCKET)" >/dev/null 2>&1; then
    gc storage buckets create "gs://$(cfg GCS_BUCKET)" \
      --location="$region" \
      --uniform-bucket-level-access \
      --public-access-prevention >/dev/null
  fi
  gc storage buckets update "gs://$(cfg GCS_BUCKET)" --update-labels="$labels" >/dev/null
  local lc_file
  lc_file="$(mktemp)"; TMP_FILES+=("$lc_file")
  cat > "$lc_file" <<'JSON'
{"rule": [{"action": {"type": "Delete"}, "condition": {"age": 90}}]}
JSON
  gc storage buckets update "gs://$(cfg GCS_BUCKET)" --lifecycle-file="$lc_file" >/dev/null
  retry_iam gc storage buckets add-iam-policy-binding "gs://$(cfg GCS_BUCKET)" \
    --member="serviceAccount:${live_sa}" --role="roles/storage.objectAdmin"

  echo "==> [6/8] Provisioning dedicated Model Armor template & dataset IAM"
  if [[ -x "${SOLUTION_ROOT}/.venv/bin/python" ]]; then
    (
      cd "$SOLUTION_ROOT"
      export GCP_PROJECT_ID="$proj" GCP_REGION="$region" APP_ENV="$(cfg APP_ENV)" \
             BQ_DATASET="$(cfg BQ_DATASET)" BQ_LOCATION="$(cfg BQ_LOCATION)" \
             MODEL_ARMOR_LOCATION="$(cfg MODEL_ARMOR_LOCATION)" \
             MODEL_ARMOR_TEMPLATE_ID="$(cfg MODEL_ARMOR_TEMPLATE_ID)" DEMO_MODE=live
      PYTHONPATH=backend .venv/bin/python scripts/provision_model_armor.py --apply
      if [[ "$SKIP_SEED" != "true" ]]; then
        PYTHONPATH=backend .venv/bin/python scripts/seed_data.py --pack all
      fi
    )
  fi
  # Dataset-scoped BigQuery access for runtime SA (never project-wide dataEditor)
  python3 -c '
import json, subprocess, sys
proj, ds, sa = sys.argv[1], sys.argv[2], sys.argv[3]
from google.cloud import bigquery
c = bigquery.Client(project=proj)
dataset = c.get_dataset(f"{proj}.{ds}")
entries = list(dataset.access_entries)
entry = bigquery.AccessEntry(role="WRITER", entity_type="userByEmail", entity_id=sa)
if entry not in entries:
    entries.append(entry)
    dataset.access_entries = entries
    c.update_dataset(dataset, ["access_entries"])
    print(f"[iam] Granted dataset WRITER on {proj}.{ds} to {sa}")
else:
    print(f"[iam] Dataset WRITER already granted on {proj}.{ds} to {sa}")
' "$proj" "$(cfg BQ_DATASET)" "$live_sa" 2>/dev/null || \
  (cd "$SOLUTION_ROOT" && PYTHONPATH=backend .venv/bin/python -c '
import sys
from google.cloud import bigquery
proj, ds, sa = sys.argv[1], sys.argv[2], sys.argv[3]
c = bigquery.Client(project=proj)
dataset = c.get_dataset(f"{proj}.{ds}")
entries = list(dataset.access_entries)
entry = bigquery.AccessEntry(role="WRITER", entity_type="userByEmail", entity_id=sa)
if entry not in entries:
    entries.append(entry)
    dataset.access_entries = entries
    c.update_dataset(dataset, ["access_entries"])
    print(f"[iam] Granted dataset WRITER on {proj}.{ds} to {sa}")
else:
    print(f"[iam] Dataset WRITER already granted on {proj}.{ds} to {sa}")
' "$proj" "$(cfg BQ_DATASET)" "$live_sa")

  echo "==> [7/8] Deploying Live Cloud Run service $(cfg SERVICE_NAME) (ACCESS_MODE=$(cfg ACCESS_MODE))"
  local live_envf access_flags iap_aud="" req_iap="false"
  live_envf="$(mktemp)"; TMP_FILES+=("$live_envf")
  if [[ "$(cfg ACCESS_MODE)" == "iap" ]]; then
    access_flags=(--no-allow-unauthenticated --iap)
    req_iap="true"
    iap_aud="/projects/${project_number}/locations/${region}/services/$(cfg SERVICE_NAME)"
  else
    access_flags=(--allow-unauthenticated --no-iap)
  fi
  write_env_yaml "$live_envf" \
    "GCP_PROJECT_ID=${proj}" \
    "GCP_REGION=${region}" \
    "APP_ENV=$(cfg APP_ENV)" \
    "BQ_DATASET=$(cfg BQ_DATASET)" \
    "BQ_LOCATION=$(cfg BQ_LOCATION)" \
    "GCS_BUCKET=$(cfg GCS_BUCKET)" \
    "DEMO_MODE=live" \
    "GRAPH_BACKEND=bigquery" \
    "MODEL_ARMOR_LOCATION=$(cfg MODEL_ARMOR_LOCATION)" \
    "MODEL_ARMOR_TEMPLATE_ID=$(cfg MODEL_ARMOR_TEMPLATE_ID)" \
    "ENABLE_CLOUD_DLP=$(cfg ENABLE_CLOUD_DLP)" \
    "REQUIRE_IAP_JWT=${req_iap}" \
    "IAP_JWT_AUDIENCE=${iap_aud}"

  gc beta run deploy "$(cfg SERVICE_NAME)" \
    --image="$image" \
    --region="$region" \
    --service-account="$live_sa" \
    "${access_flags[@]}" \
    --env-vars-file="$live_envf" \
    --labels="$labels" \
    --port=8080 \
    --cpu="$(cfg CPU)" \
    --memory="$(cfg MEMORY)" \
    --concurrency="$(cfg CONCURRENCY)" \
    --min-instances="$(cfg MIN_INSTANCES)" \
    --max-instances="$(cfg MAX_INSTANCES)" \
    --timeout="$(cfg TIMEOUT)" >/dev/null

  if [[ "$(cfg ACCESS_MODE)" == "iap" ]]; then
    echo "==> Configuring Cloud IAP service agent & allowlist"
    gc beta services identity create --service=iap.googleapis.com >/dev/null 2>&1 || true
    local iap_agent="serviceAccount:service-${project_number}@gcp-sa-iap.iam.gserviceaccount.com"
    retry_iam gc run services add-iam-policy-binding "$(cfg SERVICE_NAME)" \
      --region="$region" --member="$iap_agent" --role="roles/run.invoker"
    local member
    IFS=',' read -r -a _members <<< "$(cfg IAP_MEMBERS)"
    for member in "${_members[@]}"; do
      retry_iam gc beta iap web add-iam-policy-binding \
        --resource-type=cloud-run --service="$(cfg SERVICE_NAME)" \
        --region="$region" --member="$member" --role="roles/iap.httpsResourceAccessor"
    done
  fi

  if [[ "$(cfg DEPLOY_REPLAY)" == "true" ]]; then
    echo "==> [8/8] Deploying zero-credential Replay service $(cfg REPLAY_SERVICE_NAME)"
    ensure_sa "$(cfg REPLAY_SA_NAME)" "ACC-05 Enterprise GraphRAG Replay (Role-less)"
    local replay_envf
    replay_envf="$(mktemp)"; TMP_FILES+=("$replay_envf")
    write_env_yaml "$replay_envf" \
      "APP_ENV=$(cfg APP_ENV)" \
      "DEMO_MODE=replay" \
      "GRAPH_BACKEND=bigquery"
    gc run deploy "$(cfg REPLAY_SERVICE_NAME)" \
      --image="$image" \
      --region="$region" \
      --service-account="$replay_sa" \
      --allow-unauthenticated \
      --env-vars-file="$replay_envf" \
      --labels="$labels" \
      --port=8080 \
      --cpu=1 \
      --memory=1Gi \
      --concurrency=40 \
      --min-instances=0 \
      --max-instances=3 \
      --timeout=180 >/dev/null
  fi

  echo ""
  echo "=== Deployment Complete (do not commit live service URLs) ==="
  echo "LIVE   $(cfg SERVICE_NAME): $(gc run services describe "$(cfg SERVICE_NAME)" --region="$region" --format='value(status.url)')"
  if [[ "$(cfg DEPLOY_REPLAY)" == "true" ]]; then
    echo "REPLAY $(cfg REPLAY_SERVICE_NAME): $(gc run services describe "$(cfg REPLAY_SERVICE_NAME)" --region="$region" --format='value(status.url)')"
  fi
}

main() {
  parse_args "$@"
  load_env
  apply_defaults
  validate_config
  print_plan
  if [[ "$MODE" == "dry-run" ]]; then
    echo "DRY RUN: validated cleanly; no Google Cloud changes were made."
    return 0
  fi
  apply_deploy
}

main "$@"
