#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
FUNCTIONS_DIR="$PROJECT_DIR/functions"
WEBUI_URL="http://localhost:3000"

echo "Krull AI — Setup"
echo ""

# --- Wait for Open WebUI to be ready ---
echo "Waiting for Open WebUI to be ready..."
MAX_WAIT=60
WAITED=0
while ! curl -s -o /dev/null -w "%{http_code}" "$WEBUI_URL/api/config" | grep -q "200"; do
    sleep 2
    WAITED=$((WAITED + 2))
    if [ "$WAITED" -ge "$MAX_WAIT" ]; then
        echo "ERROR: Open WebUI did not become ready within ${MAX_WAIT}s."
        echo "       Make sure services are running: ./scripts/start.sh"
        exit 1
    fi
done
echo "[+] Open WebUI is ready"
echo ""

# --- Create admin account and get token ---
# With WEBUI_AUTH=False, we still need a token for API calls.
# Try to sign up a provisioning account, or sign in if it already exists.
ADMIN_EMAIL="admin@krull.local"
ADMIN_PASSWORD="krull-admin-setup"
ADMIN_NAME="Krull Admin"

echo "Setting up admin account for provisioning..."

# Try signup first
SIGNUP_RESP=$(curl -s -X POST "$WEBUI_URL/api/v1/auths/signup" \
    -H "Content-Type: application/json" \
    -d "{\"email\": \"$ADMIN_EMAIL\", \"password\": \"$ADMIN_PASSWORD\", \"name\": \"$ADMIN_NAME\"}" 2>/dev/null || echo "{}")

TOKEN=$(echo "$SIGNUP_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('token',''))" 2>/dev/null || echo "")

# If signup failed (account exists), try signin
if [ -z "$TOKEN" ]; then
    SIGNIN_RESP=$(curl -s -X POST "$WEBUI_URL/api/v1/auths/signin" \
        -H "Content-Type: application/json" \
        -d "{\"email\": \"$ADMIN_EMAIL\", \"password\": \"$ADMIN_PASSWORD\"}" 2>/dev/null || echo "{}")

    TOKEN=$(echo "$SIGNIN_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('token',''))" 2>/dev/null || echo "")
fi

if [ -z "$TOKEN" ]; then
    echo "ERROR: Could not authenticate with Open WebUI."
    echo "       If you've set up a different admin account, update the"
    echo "       credentials in this script or provision functions manually."
    exit 1
fi

echo "[+] Authenticated"
echo ""

# --- Install Functions ---
install_function() {
    local id="$1"
    local name="$2"
    local description="$3"
    local file="$4"
    local func_type="$5"

    echo "Installing function: $name"

    # Read the Python code
    CODE=$(python3 -c "
import json, sys
with open('$file') as f:
    print(json.dumps(f.read()))
")

    # Check if function already exists
    EXISTS=$(curl -s -o /dev/null -w "%{http_code}" \
        "$WEBUI_URL/api/v1/functions/id/$id" \
        -H "Authorization: Bearer $TOKEN")

    if [ "$EXISTS" = "200" ]; then
        # Update existing function
        RESP=$(curl -s -X POST "$WEBUI_URL/api/v1/functions/id/$id/update" \
            -H "Authorization: Bearer $TOKEN" \
            -H "Content-Type: application/json" \
            -d "{
                \"id\": \"$id\",
                \"name\": \"$name\",
                \"description\": \"$description\",
                \"content\": $CODE,
                \"type\": \"$func_type\"
            }")
        echo "  [+] Updated"
    else
        # Create new function
        RESP=$(curl -s -X POST "$WEBUI_URL/api/v1/functions/create" \
            -H "Authorization: Bearer $TOKEN" \
            -H "Content-Type: application/json" \
            -d "{
                \"id\": \"$id\",
                \"name\": \"$name\",
                \"description\": \"$description\",
                \"content\": $CODE,
                \"type\": \"$func_type\"
            }")
        echo "  [+] Created"
    fi

    # Toggle as global filter
    curl -s -X POST "$WEBUI_URL/api/v1/functions/id/$id/toggle/global" \
        -H "Authorization: Bearer $TOKEN" > /dev/null 2>&1

    echo "  [+] Enabled globally"
}

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

echo ""
echo "============================================"
echo "  Setup complete!"
echo ""
echo "  Installed filters:"
echo "    - Context Manager (auto-compaction)"
echo "    - Auto Web Search (SearXNG)"
echo "    - Kiwix Knowledge Lookup"
echo "    - Plan Mode Assistant (local model support)"
echo "    - Plan Execution Tracker (implementation guidance)"
echo "    - Skill Adapter (local model skill support)"
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
