#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

echo "Stopping Krull AI..."
docker compose -f "$PROJECT_DIR/docker-compose.yml" down
echo "All services stopped."
