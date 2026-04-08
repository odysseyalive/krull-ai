#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
ZIM_DIR="$PROJECT_DIR/zim"

# Shared progress + error log helpers (see scripts/lib/download-log.sh).
# shellcheck source=lib/download-log.sh
source "$SCRIPT_DIR/lib/download-log.sh"
export KRULL_DOWNLOAD_KIND=wikipedia

echo "Wikipedia ZIM Downloader for Krull AI"
echo ""

# Default to mini if no argument
EDITION="${1:-mini}"

case "$EDITION" in
    mini)
        FILE="wikipedia_en_100_mini_2026-01.zim"
        DESC="Wikipedia Mini (top 100 articles, ~5 MB)"
        SIZE_BYTES=5242880
        ;;
    nopic)
        FILE="wikipedia_en_all_nopic_2026-02.zim"
        DESC="Wikipedia Full - No Images (~25 GB)"
        SIZE_BYTES=26843545600
        ;;
    medicine)
        FILE="wikipedia_en_medicine_maxi_2026-01.zim"
        DESC="Wikipedia Medicine (~2 GB)"
        SIZE_BYTES=2147483648
        ;;
    full)
        FILE="wikipedia_en_all_maxi_2026-02.zim"
        DESC="Wikipedia Full with Images (~115 GB)"
        SIZE_BYTES=123480309760
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

# Declare the download in state.json so the library page can track it.
# The trap fires on any exit path (Ctrl-C, set -e abort, natural exit)
# and mirrors the actual exit code into the state file.
dl_state_begin wikipedia "$EDITION" "$DESC"
dl_state_add "$ZIM_DIR/$FILE" "$SIZE_BYTES"
_dl_on_exit() {
    local ec=$?
    if [ "$ec" -ne 0 ]; then
        dl_state_end failed || true
    else
        dl_state_end done || true
    fi
}
trap _dl_on_exit EXIT

if ! dl_run_curl "$ZIM_DIR/$FILE" \
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
