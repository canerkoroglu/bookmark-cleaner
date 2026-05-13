#!/usr/bin/env zsh
set -u

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV_PY="$ROOT_DIR/.venv/bin/python"
INPUT_FILE="$ROOT_DIR/examples/local-smoke-bookmarks.json"
OUTPUT_DIR="$ROOT_DIR/output"
MODE="${1:-full}"
MAX_URLS="${2:-3}"
MODEL_OVERRIDE="${3:-}"
MAX_API_RETRIES="${SMOKE_API_MAX_RETRIES:-4}"
BASE_BACKOFF_SECONDS="${SMOKE_API_BASE_BACKOFF_SECONDS:-1.0}"

if [[ "$MODE" == "-h" || "$MODE" == "--help" ]]; then
  echo "Usage: ./scripts/smoke_test_gemini.sh [full, api-only, cli-only] [max_urls]"
  echo "Examples:"
  echo "  ./scripts/smoke_test_gemini.sh"
  echo "  ./scripts/smoke_test_gemini.sh api-only"
  echo "  ./scripts/smoke_test_gemini.sh cli-only 5"
  echo "  ./scripts/smoke_test_gemini.sh full 3 gemini-3-flash"
  exit 0
fi

if [[ "$MODE" != "full" && "$MODE" != "api-only" && "$MODE" != "cli-only" ]]; then
  echo "Invalid mode: $MODE"
  echo "Use one of: full, api-only, cli-only"
  exit 1
fi

if [[ ! -x "$VENV_PY" ]]; then
  echo "Missing virtualenv python at $VENV_PY"
  echo "Run: python3.11 -m venv .venv && .venv/bin/python -m pip install -e ."
  exit 1
fi

if [[ -f "$ROOT_DIR/.env" ]]; then
  set -a
  source "$ROOT_DIR/.env"
  set +a
fi

MODEL="${MODEL_OVERRIDE:-${GEMINI_MODEL:-gemini-2.5-flash}}"

is_retryable_status() {
  local code="$1"
  [[ "$code" == "429" || "$code" == "503" || "$code" =~ ^5[0-9][0-9]$ ]]
}

api_ping_once() {
  local model_name="$1"
  local body_file="$2"
  local api_url="https://generativelanguage.googleapis.com/v1beta/models/${model_name}:generateContent?key=${GEMINI_API_KEY}"
  curl -sS -o "$body_file" -w "%{http_code}" "$api_url" \
    -H "Content-Type: application/json" \
    -d '{"contents":[{"parts":[{"text":"Return JSON only: {\"ok\": true}"}]}],"generationConfig":{"responseMimeType":"application/json","temperature":0.1}}'
}

model_available_for_generate_content() {
  local model_name="$1"
  local models_body_file
  models_body_file="$(mktemp)"
  local list_code
  list_code=$(curl -sS -o "$models_body_file" -w "%{http_code}" "https://generativelanguage.googleapis.com/v1beta/models?key=${GEMINI_API_KEY}")
  if [[ "$list_code" != "200" ]]; then
    echo "Model preflight: could not list models (HTTP $list_code)."
    head -c 300 "$models_body_file"
    echo
    rm -f "$models_body_file"
    return 2
  fi

  "$VENV_PY" - <<'PY' "$models_body_file" "$model_name"
import json
import sys

path = sys.argv[1]
model_name = sys.argv[2]
with open(path, "r", encoding="utf-8") as f:
    payload = json.load(f)

needle_full = f"models/{model_name}"
for model in payload.get("models", []):
    name = model.get("name", "")
    methods = set(model.get("supportedGenerationMethods", []))
    if name == needle_full and "generateContent" in methods:
        sys.exit(0)
sys.exit(1)
PY
  local available_rc=$?
  rm -f "$models_body_file"
  return "$available_rc"
}

if [[ "$MODE" == "full" || "$MODE" == "api-only" ]]; then
  echo "== Gemini API quick ping =="
  if [[ -z "${GEMINI_API_KEY:-}" ]]; then
    echo "GEMINI_API_KEY is not set; skipping direct API ping."
  else
    if ! model_available_for_generate_content "$MODEL"; then
      echo "Model preflight: '$MODEL' not found or does not support generateContent for this API key/account."
      echo "Tip: run with MODEL=gemini-2.5-flash or check ListModels for available IDs."
      exit 1
    fi

    TMP_BODY="$(mktemp)"
    HTTP_CODE=""
    for attempt in $(seq 1 "$MAX_API_RETRIES"); do
      HTTP_CODE=$(api_ping_once "$MODEL" "$TMP_BODY")
      if [[ "$HTTP_CODE" == "200" ]]; then
        break
      fi
      if is_retryable_status "$HTTP_CODE"; then
        if [[ "$attempt" -lt "$MAX_API_RETRIES" ]]; then
          sleep_for=$(( attempt * 1 ))
          echo "Transient API status $HTTP_CODE on attempt $attempt/$MAX_API_RETRIES, retrying in ${sleep_for}s..."
          sleep "$sleep_for"
          continue
        fi
      fi
      break
    done

    echo "HTTP status: $HTTP_CODE"
    head -c 400 "$TMP_BODY"
    echo
    rm -f "$TMP_BODY"
    if [[ "$HTTP_CODE" == "200" ]]; then
      echo "API ping result: OK"
    else
      echo "API ping result: FAILED (check key/model/project access)"
    fi
  fi
fi

if [[ "$MODE" == "full" || "$MODE" == "cli-only" ]]; then
  echo "== Bookmark analyzer smoke test =="
  echo "Input: $INPUT_FILE"
  echo "Max URLs: $MAX_URLS"
  echo "Model: $MODEL"
  GEMINI_MODEL="$MODEL" BA_AI_LIVE_LOGS=1 "$VENV_PY" -m bookmark_analyzer.cli \
    --input "$INPUT_FILE" \
    --output-dir "$OUTPUT_DIR" \
    --max-urls "$MAX_URLS" \
    --disable-playwright-fallback \
    --live-logs
fi

