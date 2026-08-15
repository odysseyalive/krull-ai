#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
LOGFILE="$PROJECT_DIR/data/.update-log"
STATUSFILE="$PROJECT_DIR/data/.update-status"
REG_DEBUG="$PROJECT_DIR/data/webui/registration-debug.json"
HOOKSDIR="$SCRIPT_DIR/update-hooks.d"

# Ensure data directories exist
mkdir -p "$PROJECT_DIR/data" "$PROJECT_DIR/data/webui" "$PROJECT_DIR/data/logs" "$PROJECT_DIR/data/context_jobs" "$PROJECT_DIR/data/context_store"

# Parse args
FORCE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --force) FORCE=1; shift ;;
    -h|--help) echo "Usage: $0 [--force]"; exit 0 ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

# Helper to write status JSON
write_status() {
  local phase="$1"; shift
  local msg="$*"
  cat > "$STATUSFILE" <<JSON
{"phase": "$phase", "timestamp": $(date +%s), "message": $(python3 -c "import json,sys; print(json.dumps(\"$msg\"))")}
JSON
}

# Start logging
mkdir -p "$(dirname "$LOGFILE")"
exec > >(tee "$LOGFILE") 2>&1

echo "=== Krull AI updater ==="
echo "Project: $PROJECT_DIR"
write_status started "Updater started"

cd "$PROJECT_DIR"

# Source .env if present (some compose settings depend on it)
if [ -f ".env" ]; then
  set -a; source .env; set +a || true
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
# Do not attempt to recreate the updater/sidecar itself if present; rely on compose to handle
docker compose --project-directory "$PROJECT_DIR" up -d --force-recreate

# Run setup to reprovision webui filters and other one-time steps
echo "Re-running setup.sh (provisioning)..."
"$SCRIPT_DIR/setup.sh"

# Attempt to discover Open WebUI API token from litellm/config.yaml (preferred)
TOKEN=""
if [ -f "$PROJECT_DIR/litellm/config.yaml" ]; then
  TOKEN=$(python3 - <<PY
import re,yaml,sys
p='$PROJECT_DIR/litellm/config.yaml'
try:
    import ruamel.yaml as ry
except Exception:
    ry=None
try:
    s=open(p).read()
    m=re.search(r'api_key:\s*"([^"]+)"', s)
    if m:
        print(m.group(1))
    else:
        # fallback: look for api_key: value without quotes
        m2=re.search(r'api_key:\s*([^\s\n]+)', s)
        if m2:
            print(m2.group(1))
except Exception:
    pass
PY
)
fi

# Also allow WEBUI_API_TOKEN env var in .env
if [ -z "$TOKEN" ] && [ -n "${WEBUI_API_TOKEN:-}" ]; then
  TOKEN="$WEBUI_API_TOKEN"
fi

# Run function registration if we have a token
if [ -n "$TOKEN" ]; then
  echo "Registering functions with Open WebUI..."
  mkdir -p "$PROJECT_DIR/data/webui"
  if TOKEN="$TOKEN" "$SCRIPT_DIR/register-functions.sh" > "$REG_DEBUG" 2>&1; then
    echo "Functions registered successfully. Debug output at $REG_DEBUG"
  else
    echo "Function registration failed; debug at $REG_DEBUG"
    write_status partial "Function registration failed; see $REG_DEBUG"
  fi
else
  echo "No Open WebUI token found; skipping automatic function registration."
  echo "If you want automated registration, set api_key in litellm/config.yaml or WEBUI_API_TOKEN in .env"
fi

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

# Health checks
echo "Performing health checks..."
HEALTH_OK=1
# Open WebUI
if docker exec krull-webui curl -s -o /dev/null -w "%{http_code}" http://localhost:8080/api/config 2>/dev/null | grep -q "200"; then
  echo "Open WebUI: OK"
else
  echo "Open WebUI: FAIL"; HEALTH_OK=0
fi
# LiteLLM
if curl -s -o /dev/null -w "%{http_code}" http://localhost:4000/ 2>/dev/null | grep -q "200"; then
  echo "LiteLLM: OK"
else
  echo "LiteLLM: FAIL"; HEALTH_OK=0
fi
# SSE proxy
if curl -s -o /dev/null -w "%{http_code}" http://localhost:4001/api/health 2>/dev/null | grep -q "200"; then
  echo "SSE proxy: OK"
else
  echo "SSE proxy: endpoint /api/health not healthy or not present — try /api/config as fallback"
  if curl -s -o /dev/null -w "%{http_code}" http://localhost:4001/api/config 2>/dev/null | grep -q "200"; then
    echo "SSE proxy: OK (via /api/config)"
  else
    echo "SSE proxy: FAIL"; HEALTH_OK=0
  fi
fi
# Ollama
if curl -s -o /dev/null -w "%{http_code}" http://localhost:11434/api/tags 2>/dev/null | grep -q "200"; then
  echo "Ollama: OK"
else
  echo "Ollama: FAIL"; HEALTH_OK=0
fi

if [ $HEALTH_OK -eq 1 ]; then
  write_status done "Update completed successfully"
  echo "Update finished successfully."
  exit 0
else
  write_status failed "One or more health checks failed — inspect $LOGFILE"
  echo "Update finished with failures. Check $LOGFILE and $REG_DEBUG"
  exit 2
fi
