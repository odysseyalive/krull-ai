#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

echo "Updating Krull AI..."
echo ""

echo "Pulling latest images..."
docker compose -f "$PROJECT_DIR/docker-compose.yml" pull

echo ""
echo "Recreating containers with new images..."
docker compose -f "$PROJECT_DIR/docker-compose.yml" up -d --force-recreate

echo ""
echo "Update complete. Data and models are preserved."
