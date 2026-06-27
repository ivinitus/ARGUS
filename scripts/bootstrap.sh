#!/usr/bin/env sh
set -eu

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$ROOT"

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required for the fast setup path." >&2
  echo "Install it from https://docs.astral.sh/uv/ and re-run this script." >&2
  exit 1
fi

if [ ! -f ".env.argus" ]; then
  cp scripts/env.example.sh .env.argus
  echo "Created .env.argus from scripts/env.example.sh"
fi

. ./.env.argus

: "${ARGUS_TRACKER_BASE_URL:=https://tracker.example.com}"
: "${ARGUS_PROJECT_ID:=PROJECT}"
: "${ARGUS_OUTPUT_DIR:=./output}"
: "${ARGUS_MODEL_PROVIDER:=openai}"
: "${ARGUS_MODEL_ID:=example-model}"
: "${ARGUS_MODEL_REGION:=example-region}"
: "${ARGUS_CLOUD_PROFILE:=}"
: "${ARGUS_API_BASE_URL:=}"
: "${ARGUS_API_KEY_ENV:=OPENAI_API_KEY}"
: "${ARGUS_ENV_CHECK_ENGINE:=off}"
: "${ARGUS_BATCH_CONCURRENCY:=8}"
: "${ARGUS_EXTRACTOR_CONCURRENCY:=4}"
: "${ARGUS_NOTIFY_MODE:=silent}"

cat > config.toml <<EOF
[extractor]
base_url = "${ARGUS_TRACKER_BASE_URL}"
out_dir = "${ARGUS_OUTPUT_DIR}"
project_id = "${ARGUS_PROJECT_ID}"

[auditor]
model_provider = "${ARGUS_MODEL_PROVIDER}"
model_id = "${ARGUS_MODEL_ID}"
region = "${ARGUS_MODEL_REGION}"
cloud_profile = "${ARGUS_CLOUD_PROFILE}"
api_base_url = "${ARGUS_API_BASE_URL}"
api_key_env = "${ARGUS_API_KEY_ENV}"
temperature = 0.0
consensus_enabled = false
chunk_max_parallel = 2
env_check_inline = false
env_check_sample_stride = 5
env_check_region_crop_height = 180
env_check_region_crop_bottom_height = 80
env_check_engine = "${ARGUS_ENV_CHECK_ENGINE}"

[logging]
verbose = false

[notify]
mode = "${ARGUS_NOTIFY_MODE}"
tunnel_url = ""
log_path = "~/.argus/notify.log"

[batch]
concurrency = ${ARGUS_BATCH_CONCURRENCY}
extractor_concurrency = ${ARGUS_EXTRACTOR_CONCURRENCY}
EOF

uv sync --dev

echo "Wrote config.toml and installed dependencies with uv."
echo "Edit .env.argus, then re-run scripts/bootstrap.sh to regenerate config.toml."
