#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
FUNCTIONS_DIR="$PROJECT_DIR/functions"

# API calls go through the container directly to avoid SPA routing issues
WEBUI_CONTAINER="krull-webui"
WEBUI_INTERNAL="http://localhost:8080"

echo "Krull AI — Setup"
echo ""

# --- Check container is running ---
if ! docker inspect --format='{{.State.Status}}' "$WEBUI_CONTAINER" 2>/dev/null | grep -q "running"; then
    echo "ERROR: $WEBUI_CONTAINER is not running."
    echo "       Start services first: ./scripts/start.sh"
    exit 1
fi

# --- Wait for Open WebUI API to be ready ---
echo "Waiting for Open WebUI to be ready..."
MAX_WAIT=60
WAITED=0
while ! docker exec "$WEBUI_CONTAINER" curl -s -o /dev/null -w "%{http_code}" "$WEBUI_INTERNAL/api/config" 2>/dev/null | grep -q "200"; do
    sleep 2
    WAITED=$((WAITED + 2))
    if [ "$WAITED" -ge "$MAX_WAIT" ]; then
        echo "ERROR: Open WebUI did not become ready within ${MAX_WAIT}s."
        exit 1
    fi
done
echo "[+] Open WebUI is ready"
echo ""

# --- Authenticate ---
# Open WebUI auto-creates a default admin on first launch.
# Try common default credentials, then try creating an account.
ADMIN_PASSWORD="krull-admin-setup"

echo "Setting up admin account for provisioning..."

# Try signing in with default account (created by Open WebUI on first start)
TOKEN=""
for EMAIL in "admin@localhost" "admin@krull.local"; do
    RESP=$(docker exec "$WEBUI_CONTAINER" curl -s -X POST "$WEBUI_INTERNAL/api/v1/auths/signin" \
        -H "Content-Type: application/json" \
        -d "{\"email\": \"$EMAIL\", \"password\": \"$ADMIN_PASSWORD\"}" 2>/dev/null || echo "{}")

    TOKEN=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('token',''))" 2>/dev/null || echo "")
    if [ -n "$TOKEN" ]; then
        break
    fi
done

# If no existing account works, try creating one
if [ -z "$TOKEN" ]; then
    RESP=$(docker exec "$WEBUI_CONTAINER" curl -s -X POST "$WEBUI_INTERNAL/api/v1/auths/signup" \
        -H "Content-Type: application/json" \
        -d "{\"email\": \"admin@krull.local\", \"password\": \"$ADMIN_PASSWORD\", \"name\": \"Krull Admin\"}" 2>/dev/null || echo "{}")

    TOKEN=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('token',''))" 2>/dev/null || echo "")
fi

if [ -z "$TOKEN" ]; then
    echo "ERROR: Could not authenticate with Open WebUI."
    echo "       If you've set up a different admin account, sign in at"
    echo "       http://localhost:3000 and install the functions from"
    echo "       Admin Panel > Functions manually."
    exit 1
fi

echo "[+] Authenticated"
echo ""

# --- Helper: run API calls inside the container ---
webui_api() {
    local method="$1"
    local path="$2"
    local data="$3"

    if [ -n "$data" ]; then
        docker exec "$WEBUI_CONTAINER" curl -s -X "$method" \
            "$WEBUI_INTERNAL$path" \
            -H "Authorization: Bearer $TOKEN" \
            -H "Content-Type: application/json" \
            -d "$data" 2>/dev/null
    else
        docker exec "$WEBUI_CONTAINER" curl -s -X "$method" \
            "$WEBUI_INTERNAL$path" \
            -H "Authorization: Bearer $TOKEN" \
            -H "Content-Type: application/json" 2>/dev/null
    fi
}

# --- Install Functions ---
install_function() {
    local id="$1"
    local name="$2"
    local description="$3"
    local file="$4"
    local func_type="$5"

    echo "Installing function: $name"

    # Read the Python code and JSON-encode it
    CODE=$(python3 -c "
import json
with open('$file') as f:
    print(json.dumps(f.read()))
")

    PAYLOAD="{
        \"id\": \"$id\",
        \"name\": \"$name\",
        \"description\": \"$description\",
        \"content\": $CODE,
        \"type\": \"$func_type\",
        \"meta\": {}
    }"

    # Check if function already exists
    EXISTS_RESP=$(webui_api GET "/api/v1/functions/id/$id")
    HAS_ID=$(echo "$EXISTS_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('id',''))" 2>/dev/null || echo "")

    if [ "$HAS_ID" = "$id" ]; then
        webui_api POST "/api/v1/functions/id/$id/update" "$PAYLOAD" > /dev/null
        echo "  [+] Updated"
    else
        RESP=$(webui_api POST "/api/v1/functions/create" "$PAYLOAD")
        CREATED_ID=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('id',''))" 2>/dev/null || echo "")
        if [ "$CREATED_ID" = "$id" ]; then
            echo "  [+] Created"
        else
            echo "  [-] FAILED: $(echo "$RESP" | head -c 200)"
            return 1
        fi
    fi

    # Enable globally and activate — toggles flip state, so check after each
    for toggle in "toggle/global" "toggle"; do
        CURRENT=$(webui_api GET "/api/v1/functions/id/$id" | python3 -c "
import sys,json
d = json.load(sys.stdin)
field = 'is_global' if 'global' in '$toggle' else 'is_active'
print(d.get(field, False))
" 2>/dev/null || echo "")
        if [ "$CURRENT" != "True" ]; then
            webui_api POST "/api/v1/functions/id/$id/$toggle" > /dev/null
        fi
    done

    echo "  [+] Enabled globally + activated"
}

install_function \
    "truth_guard" \
    "Truth Guard" \
    "Injects honesty rules: no fabrication, ask when unsure, flag uncertainty, push back when wrong" \
    "$FUNCTIONS_DIR/truth_guard.py" \
    "filter"

install_function \
    "current_date" \
    "Current Date & Time" \
    "Injects today's date and current time into every request" \
    "$FUNCTIONS_DIR/current_date.py" \
    "filter"

install_function \
    "context_manager" \
    "Context Manager" \
    "Automatically compacts conversation history when approaching context limits" \
    "$FUNCTIONS_DIR/context_manager.py" \
    "filter"

install_function \
    "web_search" \
    "Auto Web Search" \
    "Automatically searches SearXNG and injects results into context" \
    "$FUNCTIONS_DIR/web_search.py" \
    "filter"

install_function \
    "kiwix_lookup" \
    "Kiwix Knowledge Lookup" \
    "Searches offline Kiwix knowledge base and injects relevant articles" \
    "$FUNCTIONS_DIR/kiwix_lookup.py" \
    "filter"

install_function \
    "map_search" \
    "Offline Map Search" \
    "Searches Photon geocoding for location queries and injects results with coordinates" \
    "$FUNCTIONS_DIR/map_search.py" \
    "filter"

install_function \
    "voice_guard" \
    "Voice Guard" \
    "Shapes output toward natural conversational voice; strips em-dashes, stock openers/closers, and AI tells" \
    "$FUNCTIONS_DIR/voice_guard.py" \
    "filter"

# --- Disable auxiliary task generation ---
# Open WebUI fires a separate /api/chat call for each of: chat title,
# chat tags, follow-up question suggestions, and chat autocomplete.
# On a single-GPU stack with a thinking-mode model loaded, each of those
# triggers a full thinking trace — turning a 1-turn answer into 4-5
# minutes of GPU work even when the answer itself was fast.
# We disable them here. Users who want any of these can re-enable them
# in Admin Panel > Settings > Interface.
echo ""
echo "Disabling auxiliary task generation (title/tags/follow-up/autocomplete)..."
TASK_CONFIG=$(webui_api GET "/api/v1/tasks/config")
if [ -n "$TASK_CONFIG" ] && echo "$TASK_CONFIG" | python3 -c "import sys,json; json.load(sys.stdin)" 2>/dev/null; then
    PATCHED=$(echo "$TASK_CONFIG" | python3 -c "
import sys, json
cfg = json.load(sys.stdin)
cfg['ENABLE_TITLE_GENERATION'] = False
cfg['ENABLE_TAGS_GENERATION'] = False
cfg['ENABLE_FOLLOW_UP_GENERATION'] = False
cfg['ENABLE_AUTOCOMPLETE_GENERATION'] = False
cfg['ENABLE_RETRIEVAL_QUERY_GENERATION'] = False
print(json.dumps(cfg))
")
    RESP=$(webui_api POST "/api/v1/tasks/config/update" "$PATCHED")
    if echo "$RESP" | python3 -c "import sys,json; json.load(sys.stdin)" 2>/dev/null >/dev/null; then
        echo "[+] Task generation disabled"
    else
        echo "[!] Task config update returned unexpected response — disable manually in"
        echo "    Admin Panel > Settings > Interface if needed."
    fi
else
    echo "[!] Could not fetch task config — disable manually in"
    echo "    Admin Panel > Settings > Interface if needed."
fi

# --- Inject default system prompt from sample_prompts/voice-profile.md ---
# First-run-only: we seed the admin user's System Prompt (ui.system) with
# the voice profile, but we never overwrite an existing prompt. Once the
# user has anything in that field — the seed we provided, a hand-written
# prompt, or a customized version of ours — setup leaves it alone. This
# keeps setup idempotent without destroying user edits.
VOICE_PROFILE="$PROJECT_DIR/sample_prompts/voice-profile.md"
if [ -f "$VOICE_PROFILE" ]; then
    echo ""
    echo "Checking default system prompt..."
    CURRENT_SETTINGS=$(webui_api GET "/api/v1/users/user/settings")
    ACTION=$(VOICE_FILE="$VOICE_PROFILE" CURRENT="$CURRENT_SETTINGS" python3 << 'PYEOF'
import json, os, sys
with open(os.environ['VOICE_FILE']) as f:
    voice = f.read().strip()
current = os.environ.get('CURRENT') or ''
try:
    settings = json.loads(current) if current.strip() else {}
except json.JSONDecodeError:
    settings = {}
if not isinstance(settings, dict):
    settings = {}
ui = settings.get('ui') if isinstance(settings.get('ui'), dict) else {}
existing = ui.get('system')
if isinstance(existing, str) and existing.strip():
    # User already has a prompt set — preserve it.
    print('SKIP')
else:
    ui['system'] = voice
    settings['ui'] = ui
    print('SET')
    print(json.dumps(settings))
PYEOF
)
    STATUS=$(echo "$ACTION" | head -n 1)
    if [ "$STATUS" = "SET" ]; then
        PATCHED_SETTINGS=$(echo "$ACTION" | tail -n +2)
        RESP=$(webui_api POST "/api/v1/users/user/settings/update" "$PATCHED_SETTINGS")
        if echo "$RESP" | python3 -c "import sys,json; json.load(sys.stdin)" 2>/dev/null >/dev/null; then
            echo "[+] Seeded default system prompt from sample_prompts/voice-profile.md"
        else
            echo "[!] System prompt update returned unexpected response — set manually in"
            echo "    Settings > General > System Prompt if needed."
        fi
    elif [ "$STATUS" = "SKIP" ]; then
        echo "[=] System prompt already set — leaving existing content untouched"
    else
        echo "[!] Could not check system prompt state — skipping injection"
    fi
else
    echo ""
    echo "[!] sample_prompts/voice-profile.md not found — skipping system prompt injection"
fi

# --- Generate API key for LiteLLM → Open WebUI connection ---
echo ""
echo "Configuring LiteLLM → Open WebUI API key..."

API_KEY=$(webui_api POST "/api/v1/auths/api_key" | python3 -c "import sys,json; print(json.load(sys.stdin).get('api_key',''))" 2>/dev/null || echo "")

if [ -n "$API_KEY" ]; then
    # Update litellm config with the real API key
    python3 << PYEOF
import re
with open('$PROJECT_DIR/litellm/config.yaml') as f:
    content = f.read()
content = re.sub(r'api_key:\s*"[^"]*"', 'api_key: "$API_KEY"', content)
content = re.sub(r'api_base:\s*http://krull-webui:8080(?!/api)', 'api_base: http://krull-webui:8080/api', content)
with open('$PROJECT_DIR/litellm/config.yaml', 'w') as f:
    f.write(content)
PYEOF
    echo "[+] LiteLLM config updated with Open WebUI API key"
    echo "    Restarting LiteLLM..."
    docker restart krull-litellm > /dev/null 2>&1
    echo "[+] LiteLLM restarted"
else
    echo "[!] Could not generate API key. LiteLLM may need manual configuration."
    echo "    Generate a key in Open WebUI: Settings > Account > API Keys"
    echo "    Then update api_key values in litellm/config.yaml"
fi

# --- Install krull-claude to ~/.local/bin ---
echo ""
echo "Installing krull-claude CLI..."

LOCAL_BIN="$HOME/.local/bin"
if [ -d "$LOCAL_BIN" ]; then
    cp "$SCRIPT_DIR/krull-claude" "$LOCAL_BIN/krull-claude"
    chmod +x "$LOCAL_BIN/krull-claude"
    echo "[+] Installed krull-claude to $LOCAL_BIN/krull-claude"
    if ! echo "$PATH" | tr ':' '\n' | grep -qx "$LOCAL_BIN"; then
        echo "    Note: $LOCAL_BIN is not in your PATH. Add it to your shell profile."
    fi
else
    echo "[!] $LOCAL_BIN does not exist. To install manually:"
    echo "    mkdir -p $LOCAL_BIN"
    echo "    cp scripts/krull-claude $LOCAL_BIN/krull-claude"
    echo "    chmod +x $LOCAL_BIN/krull-claude"
fi

# --- Sentinel: mark setup complete so start.sh won't re-run it on every boot ---
mkdir -p "$PROJECT_DIR/data"
date -Iseconds > "$PROJECT_DIR/data/.setup-complete"

echo ""
echo "============================================"
echo "  Setup complete!"
echo ""
echo "  Installed filters:"
echo "    - Truth Guard (honesty rules)"
echo "    - Current Date & Time"
echo "    - Context Manager (auto-compaction)"
echo "    - Auto Web Search (SearXNG)"
echo "    - Kiwix Knowledge Lookup"
echo "    - Offline Map Search (Photon geocoding)"
echo ""
echo "  All filters are enabled globally."
echo "  Adjust settings in Open WebUI:"
echo "    Admin Panel > Functions > [filter name] > Valves"
echo ""
echo "  Auxiliary task generation:"
echo "    - ALL DISABLED (titles, tags, follow-ups, autocomplete, retrieval-query)"
echo "    - Each would fire a separate full chat call against your loaded"
echo "      model. On a single-GPU stack with a thinking-mode model, that"
echo "      can multiply per-turn GPU time by 3-5x. Re-enable individually"
echo "      in Admin Panel > Settings > Interface if you want any back."
echo ""
echo "  Context Manager defaults:"
echo "    - Max context: 16384 tokens"
echo "    - Compacts at 75% usage"
echo "    - Preserves last 6 message pairs"
echo "    - Adjust per model in the admin panel"
echo ""
echo "  Claude Code integration:"
echo "    Run 'krull-claude' to launch Claude Code"
echo "    connected to your local stack."
echo "============================================"
