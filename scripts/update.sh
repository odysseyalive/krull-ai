#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
LOGFILE="$PROJECT_DIR/data/.update-log"
STATUSFILE="$PROJECT_DIR/data/.update-status"
HOOKSDIR="$SCRIPT_DIR/update-hooks.d"

# Ensure data directories exist
mkdir -p "$PROJECT_DIR/data" "$PROJECT_DIR/data/webui" "$PROJECT_DIR/data/logs"

# Parse args
FORCE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --force) FORCE=1; shift ;;
    -h|--help) echo "Usage: $0 [--force]"; exit 0 ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

# Helper to write status JSON. The message is handed to python via the
# environment (not string-interpolated into the source) so quotes,
# backslashes, and $ in the text can never break the JSON or the shell.
write_status() {
  local phase="$1"; shift
  local msg="$*"
  local ts
  ts=$(date +%s)
  PHASE="$phase" TS="$ts" MSG="$msg" python3 - <<'PY' > "$STATUSFILE"
import json, os
print(json.dumps({
    "phase": os.environ.get("PHASE", ""),
    "timestamp": int(os.environ.get("TS", "0")),
    "message": os.environ.get("MSG", ""),
}))
PY
}

# Start logging
mkdir -p "$(dirname "$LOGFILE")"
exec > >(tee "$LOGFILE") 2>&1

echo "=== Krull AI updater ==="
echo "Project: $PROJECT_DIR"
write_status started "Updater started"

cd "$PROJECT_DIR"

# Source .env if present. Some compose settings depend on it — notably
# COMPOSE_FILE, which selects the platform/GPU override files. A bad line
# in .env must not abort the whole update under `set -e`, hence `|| ...`.
if [ -f ".env" ]; then
  set -a
  # shellcheck disable=SC1091
  source .env || echo "Warning: .env could not be fully sourced; continuing."
  set +a
fi

echo "Fetching remote..."
git fetch origin main

# Check for local changes
dirty=$(git status --porcelain)
if [ -n "$dirty" ]; then
  echo "Local working tree has uncommitted changes."
  if [ "$FORCE" -eq 1 ]; then
    ts=$(date +%Y%m%dT%H%M%S)
    backup_branch="backup/update-$ts"
    echo "Creating backup branch: $backup_branch"
    git branch "$backup_branch"
    echo "Resetting to remote..."
    git reset --hard FETCH_HEAD
  else
    write_status aborted "Local changes detected; aborting update. Run with --force to overwrite (creates backup branch)."
    echo "Aborting. Commit/stash your changes or re-run with --force to overwrite (a backup branch will be created)."
    exit 1
  fi
fi

# Try fast-forward merge
if git merge --ff-only FETCH_HEAD; then
  echo "Fast-forwarded to remote."
else
  if [ "$FORCE" -eq 1 ]; then
    echo "Non-fast-forward; force-reset to remote (after backup)."
    git reset --hard FETCH_HEAD
  else
    write_status aborted "Remote update requires manual merge; aborting."
    echo "Remote requires merge. Resolve locally or run with --force to reset."
    exit 1
  fi
fi

# Merge .env.sample keys into .env without clobbering user values
if [ -f "$SCRIPT_DIR/merge-env.sh" ]; then
  echo "Merging environment defaults..."
  "$SCRIPT_DIR/merge-env.sh"
fi

# Pull images and rebuild services
echo "Pulling latest images..."
docker compose --project-directory "$PROJECT_DIR" pull

echo "Rebuilding locally-built services..."
docker compose --project-directory "$PROJECT_DIR" build

echo "Recreating containers with new images..."
docker compose --project-directory "$PROJECT_DIR" up -d --force-recreate

# Run any update hooks
if [ -d "$HOOKSDIR" ]; then
  echo "Running update hooks..."
  for hook in "$HOOKSDIR"/*; do
    [ -x "$hook" ] || continue
    echo "Running hook: $hook"
    if ! "$hook" >> "$LOGFILE" 2>&1; then
      echo "Hook $hook failed; continuing with update but check logs."
    fi
  done
fi

# Run setup LAST, as the final provisioning pass, so everything is
# (re)provisioned against the freshly-updated stack: Open WebUI filters,
# the LiteLLM -> Open WebUI API key, the krull-claude CLI, and the setup
# sentinel. setup.sh is the single source of truth for filter
# registration — it installs each function with the correct id, name,
# description, and filter type — which is why update.sh no longer runs a
# separate registration pass of its own.
echo "Running setup (final provisioning pass)..."
"$SCRIPT_DIR/setup.sh"

# Health checks. Each probe is retried for a short window: setup.sh
# restarts LiteLLM, and a just-recreated container may still be coming
# up, so a single-shot probe would report a healthy service as failed.
echo "Performing health checks..."
HEALTH_OK=1

# Poll a check command until it succeeds or the timeout (seconds) elapses.
wait_for() {
  local label="$1"; local timeout="$2"; shift 2
  local waited=0
  until "$@" >/dev/null 2>&1; do
    if [ "$waited" -ge "$timeout" ]; then
      echo "$label: FAIL"
      return 1
    fi
    sleep 2
    waited=$((waited + 2))
  done
  echo "$label: OK"
  return 0
}

# Open WebUI ships curl in its image; probe its config endpoint from inside the container.
check_webui()   { docker exec krull-webui curl -sf -o /dev/null http://localhost:8080/api/config; }
# LiteLLM is published on :4000 and returns 200 at the root.
check_litellm() { curl -sf -o /dev/null http://localhost:4000/; }
# The SSE proxy is published on :4001; it has no dedicated health route,
# but its catch-all returns 200 at the root.
check_proxy()   { curl -sf -o /dev/null http://localhost:4001/; }
# Ollama's port is not published to the host and its image has no curl,
# so check it from inside the container with the ollama CLI.
check_ollama()  { docker exec krull-ollama ollama list; }

wait_for "Open WebUI" 30 check_webui   || HEALTH_OK=0
wait_for "LiteLLM"    60 check_litellm || HEALTH_OK=0
wait_for "SSE proxy"  30 check_proxy   || HEALTH_OK=0
wait_for "Ollama"     30 check_ollama  || HEALTH_OK=0

if [ "$HEALTH_OK" -eq 1 ]; then
  write_status done "Update completed successfully"
  echo "Update finished successfully."
  exit 0
else
  write_status failed "One or more health checks failed — inspect $LOGFILE"
  echo "Update finished with failures. Check $LOGFILE"
  exit 2
fi
