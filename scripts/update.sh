#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# Source .env for COMPOSE_FILE so we update the correct set of services
ENV_FILE="$PROJECT_DIR/.env"
if [ -f "$ENV_FILE" ]; then
    set -a; source "$ENV_FILE"; set +a
fi

echo "Updating Krull AI..."
echo ""

echo "Pulling latest images..."
docker compose --project-directory "$PROJECT_DIR" pull

echo ""
echo "Rebuilding locally-built services (krull-home, etc.)..."
docker compose --project-directory "$PROJECT_DIR" build

echo ""
echo "Recreating containers with new images..."
docker compose --project-directory "$PROJECT_DIR" up -d --force-recreate

echo ""
echo "Re-running setup so any updated filter source is re-pushed to Open WebUI..."
"$SCRIPT_DIR/setup.sh"

echo ""
echo "Update complete. Data and models are preserved."
