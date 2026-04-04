#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
MBTILES_DIR="$PROJECT_DIR/data/mbtiles"

echo "Map Data Downloader for Krull AI"
echo ""

# OpenMapTiles regions from https://data.maptiler.com/downloads/planet/
# These are free community downloads
CATALOG=(
    "oregon|north-america/us/oregon|Oregon|~300 MB"
    "washington|north-america/us/washington|Washington|~250 MB"
    "california|north-america/us/california|California|~800 MB"
    "us-west|north-america/us-west|US West|~2 GB"
    "us|north-america/us|United States|~8 GB"
    "north-america|north-america|North America|~10 GB"
    "europe|europe|Europe|~15 GB"
    "planet|planet|Entire Planet|~80 GB"
)

print_usage() {
    echo "Usage: $0 <region>"
    echo ""
    echo "Available regions:"
    echo ""
    for entry in "${CATALOG[@]}"; do
        IFS='|' read -r key path desc size <<< "$entry"
        printf "  %-20s %s (%s)\n" "$key" "$desc" "$size"
    done
    echo ""
    echo "Map tiles are from OpenMapTiles (OpenStreetMap data)."
    echo "After downloading, restart the tile server: docker restart krull-tileserver"
    echo ""
    echo "Note: Photon geocoding data downloads automatically on first start"
    echo "based on the COUNTRY_CODE setting in docker-compose.yml (default: us)."
}

if [ $# -eq 0 ]; then
    print_usage
    exit 1
fi

REGION="$1"
FILE=""
DESC=""
SIZE=""

for entry in "${CATALOG[@]}"; do
    IFS='|' read -r key path desc size <<< "$entry"
    if [ "$key" = "$REGION" ]; then
        FILE="$path"
        DESC="$desc"
        SIZE="$size"
        break
    fi
done

if [ -z "$FILE" ]; then
    echo "Unknown region: $REGION"
    echo ""
    print_usage
    exit 1
fi

FILENAME="${REGION}.mbtiles"

echo "Downloading: $DESC ($SIZE)"
echo "Destination: $MBTILES_DIR/$FILENAME"
echo ""

if [ -f "$MBTILES_DIR/$FILENAME" ]; then
    echo "File already exists. Re-download? (y/n)"
    read -r REPLY
    if [ "$REPLY" != "y" ]; then
        echo "Cancelled."
        exit 0
    fi
fi

# Download from Geofabrik (free OpenMapTiles extracts)
curl -L -C - -o "$MBTILES_DIR/$FILENAME" \
    "https://download.geofabrik.de/${FILE}-latest.osm.pbf.mbtiles" \
    --progress-bar 2>/dev/null || \
curl -L -C - -o "$MBTILES_DIR/$FILENAME" \
    "https://data.maptiler.com/download/${FILE}.mbtiles" \
    --progress-bar

echo ""
echo "Download complete: $MBTILES_DIR/$FILENAME"
echo ""
echo "Restart the tile server to load it:"
echo "  docker restart krull-tileserver"
