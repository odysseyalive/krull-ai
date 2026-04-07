#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
ZIM_DIR="$PROJECT_DIR/zim"

echo "Wikipedia ZIM Downloader for Krull AI"
echo ""

# Default to mini if no argument
EDITION="${1:-mini}"

case "$EDITION" in
    mini)
        FILE="wikipedia_en_100_mini_2026-01.zim"
        DESC="Wikipedia Mini (top 100 articles, ~5 MB)"
        ;;
    nopic)
        FILE="wikipedia_en_all_nopic_2026-02.zim"
        DESC="Wikipedia Full - No Images (~25 GB)"
        ;;
    medicine)
        FILE="wikipedia_en_medicine_maxi_2026-01.zim"
        DESC="Wikipedia Medicine (~2 GB)"
        ;;
    full)
        FILE="wikipedia_en_all_maxi_2026-02.zim"
        DESC="Wikipedia Full with Images (~115 GB)"
        ;;
    *)
        echo "Usage: $0 [mini|nopic|medicine|full]"
        echo ""
        echo "  mini     - Top 100 articles (~5 MB) - quick test"
        echo "  nopic    - All articles, no images (~25 GB)"
        echo "  medicine - Medical articles with images (~2 GB)"
        echo "  full     - Everything with images (~115 GB)"
        exit 1
        ;;
esac

echo "Downloading: $DESC"
echo "File: $FILE"
echo "Destination: $ZIM_DIR/"
echo ""

if [ -f "$ZIM_DIR/$FILE" ]; then
    echo "File already exists. Resume download? (y/n)"
    read -r REPLY
    if [ "$REPLY" != "y" ]; then
        echo "Cancelled."
        exit 0
    fi
fi

if ! curl --fail -L -C - -o "$ZIM_DIR/$FILE" \
    "https://download.kiwix.org/zim/wikipedia/$FILE" \
    --progress-bar; then
    rm -f "$ZIM_DIR/$FILE"
    echo ""
    echo "Download failed: the URL may be stale upstream."
    exit 1
fi

echo ""
echo "Download complete: $ZIM_DIR/$FILE"
echo ""
echo "Restart Kiwix to pick it up:"
echo "  docker restart krull-kiwix"
