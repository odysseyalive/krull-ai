#!/usr/bin/env bash
set -euo pipefail
# scripts/krull-claude-session-meta.sh
# Wrapper: write per-session metadata to $HOME/.krull-session-meta-<sid>.json,
# export per-session env vars for the launched claude process, and remove the file on exit.

# Session ID. A KRULL_SESSION_ID the caller set is used as given. One
# inherited from a running session is not: krull-claude exports its ID
# together with KRULL_SESSION_OWNER_PID, the PID of that session's claude,
# so a wrapper started from inside that session's shell sees an owner PID
# that is not its own and generates a fresh ID. Otherwise it would write,
# and on exit delete, the parent session's metadata file. Inside a running
# session an explicit ID is therefore ignored too.
if [ -z "${KRULL_SESSION_ID:-}" ] ||
   { [ -n "${KRULL_SESSION_OWNER_PID:-}" ] && [ "$KRULL_SESSION_OWNER_PID" != "$$" ]; }; then
  KRULL_SESSION_ID="$(date +%s)-$$"
fi

# Effective defaults (fall back to defaults if env var not set)
KRULL_AGENT_MAX_PARALLEL="${KRULL_AGENT_MAX_PARALLEL:-${AGENT_MAX_PARALLEL:-3}}"
KRULL_AGENT_TOKEN_BUDGET="${KRULL_AGENT_TOKEN_BUDGET:-${AGENT_TOKEN_BUDGET:-4096}}"
KRULL_AGENT_TIMEOUT_SECONDS="${KRULL_AGENT_TIMEOUT_SECONDS:-${AGENT_TIMEOUT_SECONDS:-120}}"
OLLAMA_NUM_CTX="${OLLAMA_NUM_CTX:-131072}"
OLLAMA_PREFERRED_QUANT="${OLLAMA_PREFERRED_QUANT:-}"

META_FILE="${HOME}/.krull-session-meta-${KRULL_SESSION_ID}.json"

# Remove the file when the launched process exits. An EXIT trap would never
# fire: the launch below is an `exec`, which replaces this shell and keeps
# its PID ($$) through krull-claude's own exec of claude. A detached watcher
# waits for that PID to go away, however it ends, then removes the file.
# The name carries the session ID, so no other session's file is touched.
# It ignores HUP/INT/TERM so a closed terminal or a Ctrl-C cannot kill it
# first, and its stdio is /dev/null so it never holds a caller's pipe open.
_krull_launch_pid=$$
(
  trap '' HUP INT TERM
  while kill -0 "$_krull_launch_pid" 2>/dev/null; do sleep 1; done
  rm -f -- "$META_FILE" || true
) </dev/null >/dev/null 2>&1 &

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

# Export to child process environment. The exec below keeps this PID, so
# KRULL_SESSION_OWNER_PID=$$ tells krull-claude the ID is its own to keep:
# the proxy reads the ID krull-claude writes to ~/.krull-session and looks
# the metadata file up under it, so the two must be the same ID.
export KRULL_SESSION_ID
export KRULL_SESSION_OWNER_PID=$$
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
