#!/usr/bin/env sh

export ARGUS_TRACKER_BASE_URL="https://tracker.example.com"
export ARGUS_PROJECT_ID="PROJECT"
export ARGUS_OUTPUT_DIR="./output"

export ARGUS_MODEL_PROVIDER="openai"
export ARGUS_MODEL_ID="example-model"
export ARGUS_MODEL_REGION="example-region"
export ARGUS_CLOUD_PROFILE=""
export ARGUS_API_BASE_URL=""
export ARGUS_API_KEY_ENV="OPENAI_API_KEY"
export ARGUS_ENV_CHECK_ENGINE="off"

export ARGUS_BATCH_CONCURRENCY="8"
export ARGUS_EXTRACTOR_CONCURRENCY="4"
export ARGUS_NOTIFY_MODE="silent"
