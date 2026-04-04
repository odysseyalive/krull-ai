#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
TILES_DIR="$PROJECT_DIR/data/tiles"
PMTILES_BIN="$PROJECT_DIR/.local/bin/pmtiles"

# Protomaps daily planet build (PMTiles format, supports HTTP range requests)
PLANET_URL="${PROTOMAPS_PLANET_URL:-https://build.protomaps.com/20250324.pmtiles}"

echo "Map Data Downloader for Krull AI"
echo ""

# Region catalog: key|description|size|bbox (west,south,east,north)
CATALOG=(
    "oregon|Oregon|~100 MB|-124.57,41.99,-116.46,46.29"
    "washington|Washington|~100 MB|-124.85,45.54,-116.92,49.00"
    "california|California|~300 MB|-124.48,32.53,-114.13,42.01"
    "colorado|Colorado|~80 MB|-109.06,36.99,-102.04,41.00"
    "new-york|New York|~150 MB|-79.76,40.50,-71.86,45.02"
    "texas|Texas|~300 MB|-106.65,25.84,-93.51,36.50"
    "us-west|US West|~800 MB|-124.85,32.53,-102.05,49.00"
    "us|United States|~3 GB|-125.0,24.5,-66.9,49.4"
    "europe|Europe|~5 GB|-31.0,34.0,40.0,72.0"
    "planet|Entire Planet|~80 GB|"
)

print_usage() {
    echo "Usage: $0 <region>"
    echo ""
    echo "Available regions:"
    echo ""
    for entry in "${CATALOG[@]}"; do
        IFS='|' read -r key desc size bbox <<< "$entry"
        printf "  %-20s %s (%s)\n" "$key" "$desc" "$size"
    done
    echo ""
    echo "Map tiles are from Protomaps (OpenStreetMap data, PMTiles format)."
    echo "Regional extracts use HTTP range requests — only the selected area is downloaded."
    echo ""
    echo "After downloading, restart the tile server: docker restart krull-tileserver"
    echo ""
    echo "Environment variables:"
    echo "  PROTOMAPS_PLANET_URL  Override the planet PMTiles source URL"
}

install_pmtiles() {
    if [ -x "$PMTILES_BIN" ]; then
        return 0
    fi

    echo "Installing pmtiles CLI..."
    local arch
    arch="$(uname -m)"
    # go-pmtiles uses x86_64/arm64 in release filenames
    case "$arch" in
        x86_64|aarch64|arm64) ;;
        *) echo "Unsupported architecture: $arch"; exit 1 ;;
    esac

    local os
    os="$(uname -s)"  # Darwin or Linux (capitalized)

    # Fetch latest version from GitHub API
    local version
    version="$(curl -fsSL https://api.github.com/repos/protomaps/go-pmtiles/releases/latest | grep -o '"tag_name": *"[^"]*"' | cut -d'"' -f4)"
    if [ -z "$version" ]; then
        echo "Failed to fetch latest pmtiles version"
        exit 1
    fi

    local url="https://github.com/protomaps/go-pmtiles/releases/download/${version}/go-pmtiles_${version#v}_${os}_${arch}.tar.gz"

    mkdir -p "$(dirname "$PMTILES_BIN")"
    echo "Downloading pmtiles ${version} for ${os}/${arch}..."
    curl -fSL "$url" | tar -xz -C "$(dirname "$PMTILES_BIN")"
    # Binary is named 'pmtiles' inside the tarball
    if [ -f "$(dirname "$PMTILES_BIN")/go-pmtiles" ]; then
        mv "$(dirname "$PMTILES_BIN")/go-pmtiles" "$PMTILES_BIN"
    fi
    chmod +x "$PMTILES_BIN"
    echo "Installed pmtiles to $PMTILES_BIN"
    echo ""
}

if [ $# -eq 0 ]; then
    print_usage
    exit 1
fi

REGION="$1"
DESC=""
SIZE=""
BBOX=""

for entry in "${CATALOG[@]}"; do
    IFS='|' read -r key desc size bbox <<< "$entry"
    if [ "$key" = "$REGION" ]; then
        DESC="$desc"
        SIZE="$size"
        BBOX="$bbox"
        break
    fi
done

if [ -z "$DESC" ]; then
    echo "Unknown region: $REGION"
    echo ""
    print_usage
    exit 1
fi

FILENAME="${REGION}.pmtiles"

echo "Downloading: $DESC ($SIZE)"
echo "Destination: $TILES_DIR/$FILENAME"
echo "Source: $PLANET_URL"
echo ""

if [ -f "$TILES_DIR/$FILENAME" ]; then
    echo "File already exists. Re-download? (y/n)"
    read -r REPLY
    if [ "$REPLY" != "y" ]; then
        echo "Cancelled."
        exit 0
    fi
    rm "$TILES_DIR/$FILENAME"
fi

install_pmtiles
mkdir -p "$TILES_DIR"

if [ "$REGION" = "planet" ]; then
    echo "Downloading full planet file (this will take a while)..."
    curl -fSL -o "$TILES_DIR/$FILENAME" "$PLANET_URL" --progress-bar
else
    echo "Extracting $DESC region via HTTP range requests..."
    echo "Bounding box: $BBOX"
    echo ""
    "$PMTILES_BIN" extract "$PLANET_URL" "$TILES_DIR/$FILENAME" --bbox="$BBOX"
fi

echo ""
echo "Download complete: $TILES_DIR/$FILENAME"
echo ""
echo "Restart the tile server to load it:"
echo "  docker restart krull-tileserver"
