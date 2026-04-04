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
    "plan_mode_assist" \
    "Plan Mode Assistant" \
    "Reinforces plan mode instructions for local models with phase tracking and guardrails" \
    "$FUNCTIONS_DIR/plan_mode_assist.py" \
    "filter"

install_function \
    "plan_executor" \
    "Plan Execution Tracker" \
    "Tracks plan step completion during implementation and keeps the model focused" \
    "$FUNCTIONS_DIR/plan_executor.py" \
    "filter"

install_function \
    "skill_adapter" \
    "Skill Adapter" \
    "Adapts Claude Code skill instructions for local model capabilities" \
    "$FUNCTIONS_DIR/skill_adapter.py" \
    "filter"

install_function \
    "map_search" \
    "Offline Map Search" \
    "Searches Photon geocoding for location queries and injects results with coordinates" \
    "$FUNCTIONS_DIR/map_search.py" \
    "filter"

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

echo ""
echo "============================================"
echo "  Setup complete!"
echo ""
echo "  Installed filters:"
echo "    - Current Date & Time"
echo "    - Context Manager (auto-compaction)"
echo "    - Auto Web Search (SearXNG)"
echo "    - Kiwix Knowledge Lookup"
echo "    - Plan Mode Assistant (local model support)"
echo "    - Plan Execution Tracker (implementation guidance)"
echo "    - Skill Adapter (local model skill support)"
echo "    - Offline Map Search (Photon geocoding)"
echo ""
echo "  All filters are enabled globally."
echo "  Adjust settings in Open WebUI:"
echo "    Admin Panel > Functions > [filter name] > Valves"
echo ""
echo "  Context Manager defaults:"
echo "    - Max context: 16384 tokens"
echo "    - Compacts at 75% usage"
echo "    - Preserves last 6 message pairs"
echo "    - Adjust per model in the admin panel"
echo "============================================"
