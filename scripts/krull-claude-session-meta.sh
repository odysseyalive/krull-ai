#!/usr/bin/env bash
set -euo pipefail
# scripts/krull-claude-session-meta.sh
# Wrapper: write per-session metadata to $HOME/.krull-session-meta-<sid>.json,
# export per-session env vars for the launched claude process, and remove the file on exit.

# Use existing KRULL_SESSION_ID if provided, else generate one
KRULL_SESSION_ID="${KRULL_SESSION_ID:-$(date +%s)-$$}"

# Effective defaults (fall back to defaults if env var not set)
KRULL_AGENT_MAX_PARALLEL="${KRULL_AGENT_MAX_PARALLEL:-${AGENT_MAX_PARALLEL:-3}}"
KRULL_AGENT_TOKEN_BUDGET="${KRULL_AGENT_TOKEN_BUDGET:-${AGENT_TOKEN_BUDGET:-4096}}"
KRULL_AGENT_TIMEOUT_SECONDS="${KRULL_AGENT_TIMEOUT_SECONDS:-${AGENT_TIMEOUT_SECONDS:-120}}"
OLLAMA_NUM_CTX="${OLLAMA_NUM_CTX:-131072}"
OLLAMA_PREFERRED_QUANT="${OLLAMA_PREFERRED_QUANT:-}"

META_FILE="${HOME}/.krull-session-meta-${KRULL_SESSION_ID}.json"

cleanup() {
  rm -f -- "$META_FILE" || true
}
trap cleanup EXIT

# Write JSON metadata securely
python3 - <<PY > "$META_FILE"
import json, os
meta = {
  "session_id": os.environ.get("KRULL_SESSION_ID", "${KRULL_SESSION_ID}"),
  "agent_max_parallel": int(os.environ.get("KRULL_AGENT_MAX_PARALLEL", "${KRULL_AGENT_MAX_PARALLEL}")),
  "agent_token_budget": int(os.environ.get("KRULL_AGENT_TOKEN_BUDGET", "${KRULL_AGENT_TOKEN_BUDGET}")),
  "agent_timeout_seconds": int(os.environ.get("KRULL_AGENT_TIMEOUT_SECONDS", "${KRULL_AGENT_TIMEOUT_SECONDS}")),
  "ollama_num_ctx": int(os.environ.get("OLLAMA_NUM_CTX", "${OLLAMA_NUM_CTX}")),
  "ollama_preferred_quant": os.environ.get("OLLAMA_PREFERRED_QUANT", "${OLLAMA_PREFERRED_QUANT}")
}
print(json.dumps(meta))
PY

# Export to child process environment
export KRULL_SESSION_ID
export KRULL_SESSION_META="$META_FILE"
export KRULL_AGENT_MAX_PARALLEL
export KRULL_AGENT_TOKEN_BUDGET
export KRULL_AGENT_TIMEOUT_SECONDS
export OLLAMA_NUM_CTX
export OLLAMA_PREFERRED_QUANT

# Exec the real launcher: prefer krull-claude if present, fall back to claude
if command -v krull-claude >/dev/null 2>&1; then
  exec krull-claude "$@"
elif command -v claude >/dev/null 2>&1; then
  exec claude "$@"
else
  echo "WARNING: no krull-claude or claude found in PATH. Metadata written to $META_FILE"
  sleep 2
  exec /bin/sh -c "exit 1"
fi
