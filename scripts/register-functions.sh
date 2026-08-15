#!/usr/bin/env bash
set -e

# scripts/register-functions.sh
# Robust function registration for Open WebUI after API changes.
# Usage: TOKEN=<webui_token> ./scripts/register-functions.sh
# Optional env:
#  WEBUI_CONTAINER (default krull-webui)
#  WEBUI_INTERNAL (default http://localhost:8080)

WEBUI_CONTAINER="${WEBUI_CONTAINER:-krull-webui}"
WEBUI_INTERNAL="${WEBUI_INTERNAL:-http://localhost:8080}"
TOKEN="${TOKEN:-}" 

if [ -z "$TOKEN" ]; then
  echo "ERROR: WEBUI API token not provided. Run: TOKEN=<token> $0"
  exit 1
fi

# Helper for calling the webui API from inside the container
webui_api() {
  local method="$1"
  local path="$2"
  local data="$3"

  if [ -n "$data" ]; then
    docker exec "$WEBUI_CONTAINER" curl -s -X "$method" \
      "$WEBUI_INTERNAL$path" \
      -H "Authorization: Bearer $TOKEN" \
      -H "Content-Type: application/json" \
      -d "$data" 2>/dev/null || true
  else
    docker exec "$WEBUI_CONTAINER" curl -s -X "$method" \
      "$WEBUI_INTERNAL$path" \
      -H "Authorization: Bearer $TOKEN" \
      -H "Content-Type: application/json" 2>/dev/null || true
  fi
}

# Try multiple candidate endpoints for create/update/list to handle
# Open WebUI API surface changes across versions.
CREATE_ENDPOINTS=("/api/v1/functions/create" "/api/functions" "/api/v1/functions")
GET_BY_ID_ENDPOINTS=("/api/v1/functions/id/%s" "/api/functions/%s" "/api/v1/functions/%s")
UPDATE_ENDPOINTS=("/api/v1/functions/id/%s/update" "/api/functions/%s" "/api/v1/functions/%s")
TOGGLE_ENDPOINTS=("/api/v1/functions/id/%s/toggle" "/api/v1/functions/%s/toggle" "/api/v1/functions/%s/toggle")

# Find all python functions in the functions/ directory
FILES=(functions/*.py)
if [ ! -e "${FILES[0]}" ]; then
  echo "No function files found in functions/ — nothing to register."
  exit 0
fi

for file in "${FILES[@]}"; do
  if [ ! -f "$file" ]; then
    continue
  fi
  # Derive id and name from filename
  base=$(basename "$file")
  id="${base%.*}"
  name="$id"
  description="Auto-registered function $id"
  func_type="python"

  echo "Registering function: $id (from $file)"

  # Read and JSON-encode the file content safely
  CODE=$(python3 - <<PY
import json,sys
print(json.dumps(open(sys.argv[1]).read()))
PY
  "$file")

  PAYLOAD="{\"id\": \"$id\", \"name\": \"$name\", \"description\": \"$description\", \"content\": $CODE, \"type\": \"$func_type\", \"meta\": {}}"

  # Check if the function exists using candidate GET endpoints
  EXISTS=""
  for tmpl in "${GET_BY_ID_ENDPOINTS[@]}"; do
    path=$(printf "$tmpl" "$id")
    resp=$(webui_api GET "$path")
    # Check for JSON with id field
    has=$(echo "$resp" | python3 - <<PY
import sys,json
try:
    d=json.load(sys.stdin)
    print(bool(d.get('id') or d.get('name')))
except Exception:
    print('')
PY
    )
    if [ "$has" = "True" ] || [ "$has" = "true" ]; then
      EXISTS="$path"
      break
    fi
  done

  if [ -n "$EXISTS" ]; then
    echo "  Found existing via $EXISTS — attempting update"
    # Try update via candidate update endpoints
    updated=0
    for tmpl in "${UPDATE_ENDPOINTS[@]}"; do
      path=$(printf "$tmpl" "$id")
      resp=$(webui_api POST "$path" "$PAYLOAD")
      # On success many Open WebUI versions return the object with id
      ok=$(echo "$resp" | python3 - <<PY
import sys,json
try:
    d=json.load(sys.stdin)
    print(bool(d.get('id') or d.get('name')))
except Exception:
    print('')
PY
      )
      if [ "$ok" = "True" ] || [ "$ok" = "true" ]; then
        echo "  [+] Updated via $path"
        updated=1
        break
      fi
    done
    if [ $updated -eq 0 ]; then
      echo "  [!] Update failed via API; attempting create as fallback"
      created=0
      for ced in "${CREATE_ENDPOINTS[@]}"; do
        resp=$(webui_api POST "$ced" "$PAYLOAD")
        ok=$(echo "$resp" | python3 - <<PY
import sys,json
try:
    d=json.load(sys.stdin)
    print(bool(d.get('id') or d.get('name')))
except Exception:
    print('')
PY
        )
        if [ "$ok" = "True" ] || [ "$ok" = "true" ]; then
          echo "  [+] Created via $ced"
          created=1
          break
        fi
      done
      if [ $created -eq 0 ]; then
        echo "  [ERROR] Could not create or update function $id via API."
        echo "           Please enable the function manually in Open WebUI Admin Panel > Functions."
      fi
    fi
  else
    # Attempt create via candidate endpoints
    created=0
    for ced in "${CREATE_ENDPOINTS[@]}"; do
      resp=$(webui_api POST "$ced" "$PAYLOAD")
      ok=$(echo "$resp" | python3 - <<PY
import sys,json
try:
    d=json.load(sys.stdin)
    print(bool(d.get('id') or d.get('name')))
except Exception:
    print('')
PY
      )
      if [ "$ok" = "True" ] || [ "$ok" = "true" ]; then
        echo "  [+] Created via $ced"
        created=1
        break
      fi
    done
    if [ $created -eq 0 ]; then
      echo "  [ERROR] Create failed for $id across known endpoints."
      echo "          Open WebUI may have changed its functions API; please install manually."
    fi
  fi

  # Attempt to activate / enable globally if possible using an update call
  # Some Open WebUI versions support toggle endpoints; we'll try an update
  # that sets is_active/is_global via the update endpoint if present.
  for tmpl in "${UPDATE_ENDPOINTS[@]}"; do
    path=$(printf "$tmpl" "$id")
    payload_update='{"is_active":true,"is_global":true}'
    resp=$(webui_api POST "$path" "$payload_update")
    ok=$(echo "$resp" | python3 - <<PY
import sys,json
try:
    d=json.load(sys.stdin)
    # Some versions echo back the object; assume success if no error
    print(True)
except Exception:
    print('')
PY
    )
    if [ "$ok" = "True" ] || [ "$ok" = "true" ]; then
      echo "  [+] Attempted to activate $id via $path"
      break
    fi
  done

  sleep 0.2
done

echo "Function registration complete. If any functions failed, open Open WebUI Admin Panel > Functions to inspect and enable them manually."
