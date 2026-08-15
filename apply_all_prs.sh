#!/usr/bin/env bash
# apply_all_prs.sh
# Create branches, apply five focused patches, commit, push, and open DRAFT PRs.
#
# Usage: run from the repository root. Requires:
#  - git configured and able to push to origin
#  - gh (GitHub CLI) authenticated and able to create PRs
#
# This script is best-effort and prints diagnostics. Inspect the created branches before merging.
set -euo pipefail

REPO="odysseyalive/krull-ai"
ORIGIN_REMOTE="${ORIGIN_REMOTE:-origin}"

command -v git >/dev/null 2>&1 || { echo "git not found"; exit 1; }
command -v gh >/dev/null 2>&1 || { echo "gh CLI not found; please install and authenticate"; exit 1; }

if [ -n "$(git status --porcelain)" ]; then
  echo "Working tree is dirty. Please stash/commit or run from a clean working tree."
  exit 1
fi

# Helper to create branch, run a set of commands, commit, push, and open a DRAFT PR.
create_draft_pr() {
  local branch="$1"; shift
  local commit_msg="$1"; shift
  local pr_title="$1"; shift
  local pr_body="$1"; shift

  echo
  echo "=== Creating branch $branch ==="
  git checkout -b "$branch"

  # Run change commands passed as argument(s)
  eval "$@"

  git add -A
  git commit -m "$commit_msg"
  git push -u "$ORIGIN_REMOTE" "$branch"

  echo "Opening DRAFT PR for $branch..."
  gh pr create --repo "$REPO" --base main --head "$branch" \
    --title "$pr_title" --body "$pr_body" --draft --label "enhancement" || {
      echo "gh pr create failed for $branch — check gh auth and repo access"
      git checkout main
      return 1
    }
  echo "Draft PR created for $branch"
  git checkout main
}

########################################
# PR 1: krull-claude session metadata wrapper
########################################
BRANCH="feature/krull-claude-session-meta"
COMMIT_MSG="krull-claude: write session metadata file and export per-session env (wrapper)"
PR_TITLE="krull-claude: write session metadata file and export per-session env"
PR_BODY="Adds scripts/krull-claude-session-meta.sh: a small wrapper that writes per-session metadata to \$HOME/.krull-session-meta-<session_id>.json, exports the same values for the launched claude process, and removes the file on exit.

Testing steps:
1. Install or symlink scripts/krull-claude-session-meta.sh as the krull-claude launcher or run it directly.
2. Run the launcher and verify a file appears: ls -l ~/.krull-session-meta-*.json
3. Inspect the JSON keys and values.
4. Exit the session and verify the file was removed.
"

PR1_CHANGES=$(cat <<'EOF'
mkdir -p scripts
cat > scripts/krull-claude-session-meta.sh <<'SH'
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
CONTEXT_COMPACT_ENABLED="${CONTEXT_COMPACT_ENABLED:-true}"
CONTEXT_SUMMARY_MODEL="${CONTEXT_SUMMARY_MODEL:-}"
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
  "context_compact_enabled": os.environ.get("CONTEXT_COMPACT_ENABLED", "${CONTEXT_COMPACT_ENABLED}") in ("1","true","True","yes"),
  "context_summary_model": os.environ.get("CONTEXT_SUMMARY_MODEL", "${CONTEXT_SUMMARY_MODEL}"),
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
export CONTEXT_COMPACT_ENABLED
export CONTEXT_SUMMARY_MODEL
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
SH

chmod +x scripts/krull-claude-session-meta.sh

# Append TECHNICAL.md section if not already present
if ! grep -q "Session metadata file (host-side)" TECHNICAL.md 2>/dev/null || [ ! -f TECHNICAL.md ]; then
  cat >> TECHNICAL.md <<'MD'

### Session metadata file (host-side)

When a host-launched session starts (via `krull-claude`), a small per-session JSON metadata file is written to the host home directory and removed on session exit:

- Path pattern: ~/.krull-session-meta-<session_id>.json (also supports ~/.krull-session-meta.json)
- Example contents:
  {
    "session_id": "1234567890-1234",
    "agent_max_parallel": 3,
    "agent_token_budget": 4096,
    "agent_timeout_seconds": 120,
    "context_compact_enabled": true,
    "context_summary_model": "claude-haiku-4-5",
    "ollama_num_ctx": 131072,
    "ollama_preferred_quant": "q4_0"
  }

Why: The proxy and background workers read this host-side, per-session metadata (via the host bind-mount) to enforce per-session limits (agent parallelism, token budgets, per-session compaction behavior) for host-launched sessions. The metadata file is ephemeral (removed at exit) and intended to be read-only for server-side components.
MD
fi
EOF
)

create_draft_pr "$BRANCH" "$COMMIT_MSG" "$PR_TITLE" "$PR_BODY" "$PR1_CHANGES"

########################################
# PR 2: proxy session meta helper
########################################
BRANCH="feature/proxy-session-meta"
COMMIT_MSG="proxy: helper to load host session-meta and expose request-local context"
PR_TITLE="proxy: read host session-meta and expose per-session context to filters"
PR_BODY="Adds proxy/session_meta.py helper to read host-written session meta files and expose them via a contextvar for filters and request handlers to use.

Integration:
Call load_and_set(session_id) near request start (where session id is resolved) so filters can access get_current_session_meta().

Testing steps:
1. Create a temp session meta file and call load_and_set(session_id, host_home=tempdir) from a Python REPL to confirm caching and returned dict.
2. Integrate into proxy entry point and validate filters can read per-session values.
"

PR2_CHANGES=$(cat <<'PY'
mkdir -p proxy
cat > proxy/session_meta.py <<'PYCODE'
# proxy/session_meta.py
# Helper for reading host-side session meta files and exposing them via contextvar
import contextvars
import json
import os
from pathlib import Path
from time import time
from typing import Dict

# Request-local session meta
_current_session_meta = contextvars.ContextVar("krull_session_meta", default={})
# Simple in-process cache with TTL
_cache: Dict[str, Dict] = {}
_CACHE_TTL = 5.0  # seconds

def load_session_meta_for(session_id: str, host_home: str = None) -> dict:
    """
    Read the session-meta file from the host home path, cache briefly, and return a dict.
    Expects files named: $HOME/.krull-session-meta-<session_id>.json or .krull-session-meta.json
    """
    host_home = host_home or os.environ.get("KRULL_HOST_HOME") or os.environ.get("HOME")
    if not host_home:
        return {}
    key1 = f"{host_home}/.krull-session-meta-{session_id}.json"
    key2 = f"{host_home}/.krull-session-meta.json"
    for path in (key1, key2):
        # caching
        c = _cache.get(path)
        if c and (time() - c.get("_ts", 0) < _CACHE_TTL):
            return c.get("data", {})
        p = Path(path)
        if p.exists():
            try:
                data = json.loads(p.read_text())
                _cache[path] = {"data": data, "_ts": time()}
                return data
            except Exception:
                return {}
    return {}

def set_current_session_meta(meta: dict):
    _current_session_meta.set(meta)

def get_current_session_meta(default=None):
    return _current_session_meta.get(default or {})

# Convenience: load & set in one call
def load_and_set(session_id: str, host_home: str = None):
    meta = load_session_meta_for(session_id, host_home=host_home)
    set_current_session_meta(meta)
    return meta
PYCODE
PY
)

create_draft_pr "$BRANCH" "$COMMIT_MSG" "$PR_TITLE" "$PR_BODY" "$PR2_CHANGES"

########################################
# PR 3: pull-model probe for quant candidates
########################################
BRANCH="feature/pull-probe-quant"
COMMIT_MSG="pull-model: try OLLAMA_PREFERRED_QUANT candidate before base model"
PR_TITLE="pull-model: probe registry for preferred quant artifacts and try them before base"
PR_BODY="When OLLAMA_PREFERRED_QUANT is set, attempt to pull <model>-<quant> first (e.g., gemma4:e2b-q4_0) and fall back to the base model if unavailable. Works with both macOS native ollama and docker exec krull-ollama paths.

Testing steps:
1. Set OLLAMA_PREFERRED_QUANT in .env, run the pull script and confirm it attempts the quant candidate first.
2. Test both native and docker modes.
"

# Insert snippet into scripts/pull-model.sh after the first 'for MODEL in "$@"; do' line.
if [ ! -f scripts/pull-model.sh ]; then
  echo "scripts/pull-model.sh not found; creating a new file with quant candidate logic as a minimal wrapper."
  PR3_CHANGES=$(cat <<'SH'
cat > scripts/pull-model.sh <<'SHER'
#!/usr/bin/env bash
set -euo pipefail

IS_MACOS=0
if [[ "$(uname -s)" == "Darwin" ]]; then
  IS_MACOS=1
fi

if [ "$#" -lt 1 ]; then
  echo "Usage: $0 <model> [<model>...]"
  exit 1
fi

for MODEL in "$@"; do
  echo "Pulling $MODEL..."
  if [ -n "${OLLAMA_PREFERRED_QUANT:-}" ]; then
    QUANT_CAND="${MODEL}-${OLLAMA_PREFERRED_QUANT}"
    echo "Trying quant candidate: $QUANT_CAND"
    if [ "$IS_MACOS" -eq 1 ]; then
      if ollama pull "$QUANT_CAND" 2>/dev/null; then
        MODEL="$QUANT_CAND"
      else
        echo "Quant candidate not available; will pull base $MODEL"
      fi
    else
      if docker exec krull-ollama ollama pull "$QUANT_CAND" 2>/dev/null; then
        MODEL="$QUANT_CAND"
      else
        echo "Quant candidate not available; will pull base $MODEL"
      fi
    fi
  fi

  if [ "$IS_MACOS" -eq 1 ]; then
    ollama pull "$MODEL"
  else
    docker exec krull-ollama ollama pull "$MODEL"
  fi
done
SHER
chmod +x scripts/pull-model.sh
SH
)
else
  # backup original
  cp scripts/pull-model.sh scripts/pull-model.sh.bak
  # Use awk to insert snippet after the first "for MODEL in" line
  SNIPPET=''
  SNIPPET='    if [ -n "${OLLAMA_PREFERRED_QUANT:-}" ]; then\n      QUANT_CAND="${MODEL}-${OLLAMA_PREFERRED_QUANT}"\n      echo "Trying quant candidate: $QUANT_CAND"\n      if [ "$IS_MACOS" -eq 1 ]; then\n        if ollama pull "$QUANT_CAND" 2>/dev/null; then\n          MODEL="$QUANT_CAND"\n        else\n          echo "Quant candidate not available; will pull base $MODEL"\n        fi\n      else\n        if docker exec krull-ollama ollama pull "$QUANT_CAND" 2>/dev/null; then\n          MODEL="$QUANT_CAND"\n        else\n          echo "Quant candidate not available; will pull base $MODEL"\n        fi\n      fi\n    fi\n'
  awk -v snip="$SNIPPET" '
  BEGIN{inserted=0}
  {
    print $0
    if (!inserted && $0 ~ /for[[:space:]]+MODEL[[:space:]]+in/){
      print snip
      inserted=1
    }
  }
  ' scripts/pull-model.sh.bak > scripts/pull-model.sh
  chmod +x scripts/pull-model.sh
  PR3_CHANGES="patched existing scripts/pull-model.sh (backup at scripts/pull-model.sh.bak)"
fi

create_draft_pr "$BRANCH" "$COMMIT_MSG" "$PR_TITLE" "$PR_BODY" "$PR3_CHANGES"

########################################
# PR 4: persist pulled model key into .env after retune
########################################
BRANCH="feature/write-pulledname-env"
COMMIT_MSG="modelInstaller: persist pulled model key to .env after successful retune"
PR_TITLE="write pulled model key into .env when activating"
PR_BODY="After a successful pull+retune, write the canonical pulled model name into OLLAMA_MODEL in .env (create a timestamped backup of .env first). Non-fatal on failure.

Testing steps:
1. Pull and retune a model successfully; inspect .env to confirm OLLAMA_MODEL updates.
2. Verify .env backup exists when update occurred.
"

# Modify krull-home/server/lib/modelInstaller.ts: insert safe write after retuneModel call.
if [ -f krull-home/server/lib/modelInstaller.ts ]; then
  cp krull-home/server/lib/modelInstaller.ts krull-home/server/lib/modelInstaller.ts.bak
  awk '
  BEGIN{inserted=0}
  {
    print $0
    if (!inserted && $0 ~ /retuneModel\(/) {
      # after the line that calls retuneModel, insert the env write snippet
      print "  try {"
      print "    const envPath = path.join(REPO, \".env\");"
      print "    const parsed = await readEnvFile(envPath);"
      print "    setValue(parsed, \"OLLAMA_MODEL\", pulledName || modelKey || \"\");"
      print "    const backup = envPath + ".bak." + Date.now().toString();"
      print "    await fs.copyFile(envPath, backup);"
      print "    await writeEnvFile(envPath, parsed);"
      print "    console.log('[modelInstaller] wrote pulled model key to .env ->', parsed['OLLAMA_MODEL']);"
      print "  } catch (err) {"
      print "    console.warn('[modelInstaller] could not write pulled model to .env:', err);"
      print "  }"
      inserted=1
    }
  }
  ' krull-home/server/lib/modelInstaller.ts.bak > krull-home/server/lib/modelInstaller.ts
  PR4_CHANGES="patched krull-home/server/lib/modelInstaller.ts (backup at krull-home/server/lib/modelInstaller.ts.bak)"
else
  echo "krull-home/server/lib/modelInstaller.ts not found — skipping PR4 file edit creation."
  PR4_CHANGES="skipped: file missing"
fi

create_draft_pr "$BRANCH" "$COMMIT_MSG" "$PR_TITLE" "$PR_BODY" "$PR4_CHANGES"

########################################
# PR 5: docs: add post-update checklist to TECHNICAL.md
########################################
BRANCH="docs/update-checklist"
COMMIT_MSG="docs: add post-update checklist and rollback steps to TECHNICAL.md"
PR_TITLE="docs: update TECHNICAL.md with post-update checklist & rollback steps"
PR_BODY="Adds a post-update checklist section documenting how to run ./scripts/update.sh, where logs and status are written, and rollback steps. Useful for admins running updates.

Testing steps:
1. Read the appended checklist in TECHNICAL.md and verify commands match your layout.
"

PR5_CHANGES=$(cat <<'MD'
# Append the post-update checklist to TECHNICAL.md if not already present
if ! grep -q "Post-update checklist (added" TECHNICAL.md 2>/dev/null || [ ! -f TECHNICAL.md ]; then
  cat >> TECHNICAL.md <<'TXT'

## Post-update checklist (added 2026-08-15)

When you run ./scripts/update.sh or use the homepage Update button, follow this short checklist:

1. Ensure data directories exist:
   mkdir -p data/context_jobs data/context_store logs

2. Run the updater:
   - Non-destructive: ./scripts/update.sh
   - Force (creates backup branch): ./scripts/update.sh --force

3. Check logs and status:
   - cat data/.update-status
   - tail -n 200 data/.update-log

4. If Open WebUI function registration failed:
   - Inspect data/webui/registration-debug.json
   - Run manual registration:
     TOKEN=<token> ./scripts/register-functions.sh

5. Verify summary worker is running (if used) and that jobs appear in data/context_jobs.

6. If you used --force and need to rollback:
   - git reset --hard backup/update-YYYYMMDDT...

7. If migration scripts are required for a release, drop them into scripts/update-hooks.d/; they will run after the update.

These notes explain the updater's safe defaults: .env is merged (new keys appended), update hooks are executed, and health checks are run at the end. If a step fails, the updater writes data/.update-status with a helpful message.
TXT
fi
MD
)

create_draft_pr "$BRANCH" "$COMMIT_MSG" "$PR_TITLE" "$PR_BODY" "$PR5_CHANGES"

echo
echo "All done. Created draft PR branches and opened draft PRs (if gh API succeeded)."
echo "Inspect the branches, review changes, and convert PRs to ready when you wish."
