#!/usr/bin/env bash
# Repository hygiene & 3-layer README synchronization check.
# Run before every commit/push: ./scripts/check_repo_hygiene.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

FAIL=0

echo "[1/4] Verifying no local-only internal planning docs are staged or tracked..."
if git ls-files -z -- '_internal/*' '*SOLUTIONS_BLUEPRINT*' | tr -d '\0' | grep -q .; then
  echo "ERROR: Internal tracking docs (_internal/ or *SOLUTIONS_BLUEPRINT*) must never be tracked or pushed!"
  git ls-files -- '_internal/*' '*SOLUTIONS_BLUEPRINT*'
  FAIL=1
fi

echo "[2/4] Scanning solutions/, scripts/, and root README.md for secrets, local paths, and live project IDs..."
ADC_PAT="application_default""_credentials\.json"
BASE_PATTERN="${ADC_PAT}|-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----|AIza[0-9A-Za-z_-]{35}|projects/[0-9]{6,}|[a-z0-9-]+-[a-z0-9]{8,}-[a-z]{2}\.a\.run\.app|/(Users|usr/local/[^/]+/home)/"

if [ -n "${EXTRA_LEAK_REGEX:-}" ]; then
  SCAN_PATTERN="${BASE_PATTERN}|${EXTRA_LEAK_REGEX}"
else
  SCAN_PATTERN="${BASE_PATTERN}"
fi

# Default scope: active solutions/, scripts/, and root README.md (pass --all to also audit legacy Demos/ & AI & Machine Learning/)
if [ "${1:-}" = "--all" ]; then
  FILE_LIST_CMD=(git ls-files -z -co --exclude-standard)
else
  FILE_LIST_CMD=(git ls-files -z -co --exclude-standard -- README.md solutions scripts)
fi

while IFS= read -r -d '' file; do
  case "$file" in
    scripts/check_repo_hygiene.sh|*.png|*.jpg|*.jpeg|*.gif|*.pdf|*.zip|*.parquet|*.mp3|*.mp4|*.wav)
      continue
      ;;
  esac
  if grep -nIE "$SCAN_PATTERN" "$file" >/dev/null 2>&1; then
    echo "LEAK MATCH in $file:"
    grep -nIE "$SCAN_PATTERN" "$file" || true
    FAIL=1
  fi
done < <("${FILE_LIST_CMD[@]}")

echo "[3/4] Verifying parent README.md <-> solutions/README.md link..."
for required in "README.md" "solutions/README.md"; do
  if [ ! -f "$required" ]; then
    echo "MISSING required documentation file: $required"
    FAIL=1
  fi
done

if ! grep -q "solutions/README.md" README.md; then
  echo "ERROR: Root README.md must link to solutions/README.md"
  FAIL=1
fi

echo "[4/4] Verifying 3-layer README sync for every implemented solution under solutions/*/..."
if [ -d "solutions" ]; then
  for dir in solutions/*/; do
    [ -d "$dir" ] || continue
    slug="$(basename "$dir")"
    case "$slug" in
      _kit|_template|_internal|.* ) continue ;;
    esac
    if [ ! -f "${dir}README.md" ]; then
      echo "ERROR: Solution '${slug}' is missing local ${dir}README.md"
      FAIL=1
    fi
    if ! grep -q "${slug}" solutions/README.md; then
      echo "ERROR: Solution '${slug}' is not referenced in solutions/README.md"
      FAIL=1
    fi
    if ! grep -q "${slug}" README.md; then
      echo "ERROR: Solution '${slug}' is not referenced in parent root README.md"
      FAIL=1
    fi
  done
fi

if [ "$FAIL" -ne 0 ]; then
  echo "Hygiene check FAILED."
  exit 1
fi

echo "All hygiene and 3-layer README sync checks PASSED."
