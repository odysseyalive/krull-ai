#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
TILES_DIR="$PROJECT_DIR/data/tiles"
PMTILES_BIN="$PROJECT_DIR/.local/bin/pmtiles"

# Shared progress + error log helpers (see scripts/lib/download-log.sh).
# shellcheck source=lib/download-log.sh
source "$SCRIPT_DIR/lib/download-log.sh"
export KRULL_DOWNLOAD_KIND=maps

# Protomaps daily planet build (PMTiles format, supports HTTP range requests)
PLANET_URL="${PROTOMAPS_PLANET_URL:-https://build.protomaps.com/20250324.pmtiles}"

# Terrain DEM tiles (terrarium encoding)
TERRAIN_GLOBAL_URL="${TERRAIN_GLOBAL_URL:-https://r2-public.protomaps.com/protomaps-sample-datasets/terrarium_z9.pmtiles}"

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

# NOAA NCDS nautical chart sections: id|description|size_mb|bbox (west,south,east,north)
# Source: https://distribution.charts.noaa.gov/ncds/
# Updated weekly by NOAA. Real nautical charts with bathymetry, shipping lanes, buoys, etc.
NCDS_SECTIONS=(
    "ncds_01a|ME/NH coast|596|-76.68,42.80,-69.15,45.50"
    "ncds_01b|ME coast (east)|479|-69.15,42.80,-67.30,45.50"
    "ncds_01c|ME/NB border|138|-67.30,42.80,-64.95,45.50"
    "ncds_02a|CT/NY coast|323|-76.68,41.10,-71.08,42.80"
    "ncds_02b|MA/RI coast|508|-71.08,41.10,-64.95,42.80"
    "ncds_03|NJ/NY coast|577|-76.68,39.73,-64.95,41.10"
    "ncds_04|DE/MD/VA coast|572|-77.81,38.23,-64.95,39.73"
    "ncds_05|VA/NC coast|534|-77.81,35.50,-64.95,38.23"
    "ncds_06|SC/NC coast|374|-82.20,32.54,-64.95,35.50"
    "ncds_07|GA/SC/FL coast|611|-82.20,28.10,-66.70,32.54"
    "ncds_08|S Florida/Keys|355|-80.60,23.10,-66.70,28.10"
    "ncds_09|Caribbean|593|-80.60,8.64,-64.20,23.10"
    "ncds_10|W Caribbean/FL|593|-89.07,17.60,-80.60,28.10"
    "ncds_11|FL Gulf coast|562|-89.07,28.10,-82.20,33.93"
    "ncds_12|LA/MS coast|419|-92.46,29.33,-89.07,33.93"
    "ncds_13|Gulf south|363|-92.46,17.60,-89.07,29.33"
    "ncds_14|TX coast|606|-98.29,17.60,-92.46,33.93"
    "ncds_15|Lake Erie|351|-83.90,41.10,-76.68,42.80"
    "ncds_16|Lake Ontario|309|-83.93,42.80,-76.68,47.20"
    "ncds_17a|Lake Michigan N|494|-88.93,43.80,-83.90,46.00"
    "ncds_17b|Lake Michigan S|174|-88.93,41.38,-83.90,43.80"
    "ncds_18|Lake Superior|404|-92.70,46.00,-83.90,49.46"
    "ncds_19a|S California|407|-123.50,15.00,-116.17,33.78"
    "ncds_19b|C California|416|-123.50,33.78,-113.91,37.17"
    "ncds_19c|N California|230|-123.50,37.17,-116.17,39.05"
    "ncds_19d|Pacific offshore|118|-144.00,15.00,-123.50,39.05"
    "ncds_20a|OR coast|425|-144.00,39.05,-116.17,44.70"
    "ncds_20b|WA coast S|371|-129.92,44.70,-116.20,47.01"
    "ncds_20c|WA/AK coast|632|-129.92,47.01,-116.33,60.33"
    "ncds_21|Pacific NW offshore|35|-138.09,44.70,-129.92,52.49"
    "ncds_22a|SE Alaska A|777|-138.09,52.49,-129.92,55.78"
    "ncds_22b|SE Alaska B|746|-138.09,55.78,-129.92,56.83"
    "ncds_23a|SE Alaska C|495|-138.09,56.83,-129.92,57.73"
    "ncds_23b|SE Alaska D|527|-138.09,57.73,-129.92,61.25"
    "ncds_24a|S Alaska A|555|-148.80,44.70,-138.09,61.58"
    "ncds_24b|S Alaska B|566|-152.76,44.70,-148.80,61.58"
    "ncds_25a|SW Alaska A|476|-159.00,44.70,-152.76,61.58"
    "ncds_25b|SW Alaska B|520|-165.75,44.70,-159.00,61.58"
    "ncds_26a|W Alaska A|575|-173.40,44.70,-165.75,61.58"
    "ncds_26b|W Alaska B|436|-180.00,44.70,-173.40,61.58"
    "ncds_27|N Alaska/Arctic|503|-180.00,61.58,-134.00,81.60"
    "ncds_28a|Hawaii W|406|-180.00,15.00,-162.68,44.70"
    "ncds_28b|Hawaii E|587|-162.68,15.00,-144.00,44.70"
    "ncds_29|Pacific Islands|138|-180.00,-17.56,-154.00,15.00"
    "ncds_30|W Aleutians|470|165.67,47.87,180.00,68.00"
    "ncds_31a|W Pacific A|555|131.05,0.00,154.08,26.00"
    "ncds_31b|W Pacific B|605|154.08,0.00,173.53,26.00"
)

NCDS_BASE_URL="https://distribution.charts.noaa.gov/ncds/mbtiles"

# User-friendly nautical region aliases mapping to NCDS sections
# key|description|size_estimate|ncds_sections (space-separated)
NAUTICAL_CATALOG=(
    "us-west-coast|US West Coast (CA to WA)|~1.5 GB|ncds_19a ncds_19b ncds_19c ncds_20a ncds_20b"
    "us-east-coast|US East Coast (ME to FL)|~4.5 GB|ncds_01a ncds_01b ncds_02a ncds_02b ncds_03 ncds_04 ncds_05 ncds_06 ncds_07 ncds_08"
    "gulf-of-mexico|Gulf of Mexico (FL to TX)|~2.0 GB|ncds_10 ncds_11 ncds_12 ncds_13 ncds_14"
    "great-lakes|Great Lakes|~1.7 GB|ncds_15 ncds_16 ncds_17a ncds_17b ncds_18"
    "alaska|Alaska|~6.0 GB|ncds_20c ncds_22a ncds_22b ncds_23a ncds_23b ncds_24a ncds_24b ncds_25a ncds_25b ncds_26a ncds_26b ncds_27"
    "hawaii|Hawaii & Pacific|~1.1 GB|ncds_28a ncds_28b ncds_29"
    "california|California|~1.1 GB|ncds_19a ncds_19b ncds_19c"
    "pacific-nw|Pacific Northwest (OR/WA)|~800 MB|ncds_20a ncds_20b"
    "new-england|New England (ME to CT)|~1.9 GB|ncds_01a ncds_01b ncds_01c ncds_02a ncds_02b"
    "mid-atlantic|Mid-Atlantic (NY to VA)|~1.7 GB|ncds_03 ncds_04 ncds_05"
    "southeast|Southeast (NC to FL)|~1.3 GB|ncds_06 ncds_07 ncds_08"
    "florida|Florida (all coasts)|~1.5 GB|ncds_07 ncds_08 ncds_10 ncds_11"
)

# FAA VFR Sectional chart edition date (updated every 56 days)
FAA_EDITION="${FAA_EDITION:-03-19-2026}"
FAA_BASE_URL="https://aeronav.faa.gov/visual/${FAA_EDITION}/sectional-files"

# FAA VFR Sectional charts: filename|description|approx_size_mb|bbox (west,south,east,north)
FAA_SECTIONS=(
    "Seattle|WA, N OR, N ID|90|-128.0,44.5,-114.0,50.0"
    "Klamath_Falls|S OR, N CA, NV|85|-128.0,39.0,-116.0,45.0"
    "San_Francisco|C CA, NV|78|-128.0,34.0,-118.0,40.0"
    "Los_Angeles|S CA, AZ|74|-122.0,30.0,-113.0,36.0"
    "Las_Vegas|NV, AZ, UT|83|-119.0,33.0,-110.0,39.0"
    "Salt_Lake_City|UT, ID, WY|90|-118.0,38.0,-107.0,44.0"
    "Great_Falls|MT, ND, WY|101|-116.0,44.0,-102.0,50.0"
    "Billings|MT, ND, SD, WY|53|-112.0,42.0,-100.0,48.0"
    "Denver|CO, KS, NE, WY|81|-110.0,36.0,-100.0,42.0"
    "Cheyenne|CO, WY, NE|78|-110.0,38.0,-100.0,44.0"
    "Phoenix|AZ, NM|100|-116.0,30.0,-106.0,36.0"
    "Albuquerque|NM, CO, TX|65|-110.0,30.0,-100.0,37.0"
    "El_Paso|TX, NM|57|-108.0,28.0,-98.0,34.0"
    "Dallas-Ft_Worth|TX, OK, AR|66|-102.0,30.0,-92.0,36.0"
    "San_Antonio|TX|62|-104.0,26.0,-94.0,32.0"
    "Houston|TX, LA|49|-100.0,26.0,-90.0,32.0"
    "New_Orleans|LA, MS|49|-96.0,26.0,-86.0,32.0"
    "Memphis|TN, AR, MS, MO|69|-96.0,32.0,-84.0,38.0"
    "Kansas_City|MO, KS, OK|62|-100.0,34.0,-90.0,40.0"
    "Omaha|NE, IA, MO, SD|72|-102.0,38.0,-92.0,44.0"
    "Twin_Cities|MN, WI, IA|62|-98.0,42.0,-86.0,48.0"
    "Chicago|IL, IN, WI|71|-92.0,38.0,-82.0,44.0"
    "Green_Bay|WI, MI, MN|58|-92.0,42.0,-82.0,48.0"
    "St_Louis|MO, IL, AR|71|-96.0,34.0,-86.0,40.0"
    "Wichita|KS, OK|55|-102.0,34.0,-94.0,40.0"
    "Detroit|MI, OH, PA|73|-88.0,38.0,-78.0,44.0"
    "Cincinnati|OH, KY, IN|85|-90.0,36.0,-80.0,42.0"
    "Atlanta|GA, AL, SC|88|-90.0,30.0,-78.0,36.0"
    "Charlotte|NC, SC, TN|57|-86.0,32.0,-76.0,38.0"
    "Jacksonville|FL, GA|53|-86.0,26.0,-76.0,32.0"
    "Miami|FL|38|-84.0,24.0,-78.0,28.0"
    "Washington|MD, VA, WV, PA|58|-82.0,36.0,-72.0,42.0"
    "New_York|NY, NJ, PA, CT|85|-78.0,38.0,-68.0,44.0"
    "Lake_Huron|MI, Ontario|56|-86.0,42.0,-78.0,48.0"
    "Montreal|Quebec|68|-80.0,42.0,-68.0,48.0"
    "Halifax|Nova Scotia|48|-72.0,40.0,-60.0,48.0"
    "Brownsville|S TX|54|-102.0,24.0,-94.0,28.0"
    "Hawaiian_Islands|Hawaii|27|-162.0,18.0,-154.0,23.0"
)

# User-friendly aeronautical region aliases mapping to FAA sectional charts
# key|description|size_estimate|faa_charts (space-separated)
AERO_CATALOG=(
    "pacific-nw|Pacific Northwest (OR/WA)|~175 MB|Seattle Klamath_Falls"
    "california|California|~235 MB|San_Francisco Los_Angeles Klamath_Falls"
    "southwest|Southwest (AZ/NV/UT)|~275 MB|Las_Vegas Phoenix Salt_Lake_City"
    "mountain|Mountain West (CO/WY/MT)|~310 MB|Denver Cheyenne Billings Great_Falls"
    "texas|Texas|~285 MB|Dallas-Ft_Worth San_Antonio Houston El_Paso Brownsville"
    "midwest|Midwest (IL/MO/MN/IA)|~325 MB|Chicago Kansas_City Twin_Cities Omaha St_Louis"
    "southeast|Southeast (GA/FL/SC)|~235 MB|Atlanta Jacksonville Miami Charlotte"
    "northeast|Northeast (NY/PA/MD)|~265 MB|New_York Washington Detroit Cincinnati"
    "hawaii|Hawaii|~27 MB|Hawaiian_Islands"
)

print_usage() {
    echo "Usage: $0 [command] [options]"
    echo ""
    echo "Commands:"
    echo "  <region>                Download all maps for a region (base + terrain + nautical + aero)"
    echo "  --base-only <region>    Download only base OSM tiles for a region"
    echo "  --terrain global        Download global terrain/hillshade data (z0-9, ~700 MB)"
    echo "  --terrain <region>      Download high-detail terrain for a region (z0-12)"
    echo "  --nautical <region>     Download NOAA nautical charts for a coastal region"
    echo "  --aeronautical <region> Download FAA VFR sectional charts for a region"
    echo "  list                    Show all available regions and what's downloaded"
    echo "  status                  Show downloaded tiles with file sizes"
    echo "  remove <filename>       Remove a downloaded tile file"
    echo ""
    echo "Base map regions:"
    echo ""
    for entry in "${CATALOG[@]}"; do
        IFS='|' read -r key desc size bbox <<< "$entry"
        printf "  %-20s %s (%s)\n" "$key" "$desc" "$size"
    done
    echo ""
    echo "Nautical regions (NOAA charts):"
    echo ""
    for entry in "${NAUTICAL_CATALOG[@]}"; do
        IFS='|' read -r key desc size sections <<< "$entry"
        printf "  %-20s %s (%s)\n" "$key" "$desc" "$size"
    done
    echo ""
    echo "Aeronautical regions (FAA VFR Sectional Charts):"
    echo ""
    for entry in "${AERO_CATALOG[@]}"; do
        IFS='|' read -r key desc size charts <<< "$entry"
        printf "  %-20s %s (%s)\n" "$key" "$desc" "$size"
    done
    echo ""
    echo "Map tiles are from Protomaps (OpenStreetMap data, PMTiles format)."
    echo "Terrain data is from AWS Terrain Tiles (terrarium encoding)."
    echo "Nautical data is from NOAA Chart Display Service (NCDS), updated weekly."
    echo "Aeronautical data is from FAA VFR Sectional Charts, updated every 56 days."
    echo ""
    echo "After downloading, restart the tile server: docker restart krull-tileserver"
    echo ""
    echo "Environment variables:"
    echo "  PROTOMAPS_PLANET_URL  Override the planet PMTiles source URL"
    echo "  TERRAIN_GLOBAL_URL    Override the global terrain PMTiles source URL"
    echo "  FAA_EDITION           Override the FAA chart edition date (e.g., 03-19-2026)"
}

install_pmtiles() {
    if [ -x "$PMTILES_BIN" ]; then
        return 0
    fi

    echo "Installing pmtiles CLI..."
    local arch
    arch="$(uname -m)"
    case "$arch" in
        x86_64|aarch64|arm64) ;;
        *) echo "Unsupported architecture: $arch"; exit 1 ;;
    esac

    local os
    os="$(uname -s)"

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
    if [ -f "$(dirname "$PMTILES_BIN")/go-pmtiles" ]; then
        mv "$(dirname "$PMTILES_BIN")/go-pmtiles" "$PMTILES_BIN"
    fi
    chmod +x "$PMTILES_BIN"
    echo "Installed pmtiles to $PMTILES_BIN"
    echo ""
}

download_base_tiles() {
    local REGION="$1"
    local DESC="" SIZE="" BBOX=""

    for entry in "${CATALOG[@]}"; do
        IFS='|' read -r key desc size bbox <<< "$entry"
        if [ "$key" = "$REGION" ]; then
            DESC="$desc"; SIZE="$size"; BBOX="$bbox"
            break
        fi
    done

    if [ -z "$DESC" ]; then
        echo "Unknown base map region: $REGION"
        echo "Run '$0 list' to see available regions."
        exit 1
    fi

    local FILENAME="${REGION}.pmtiles"

    echo "Downloading base tiles: $DESC ($SIZE)"
    echo "Destination: $TILES_DIR/$FILENAME"
    echo ""

    if [ -f "$TILES_DIR/$FILENAME" ]; then
        if [ -t 0 ]; then
            # Interactive terminal — prompt before wiping.
            echo "File already exists. Re-download? (y/n)"
            read -r REPLY
            if [ "$REPLY" != "y" ]; then
                echo "Cancelled."
                return 0
            fi
            rm "$TILES_DIR/$FILENAME"
        else
            # Non-interactive (installer / CI / piped stdin). A file on
            # disk from a previous run means we should RESUME, not wipe
            # and restart. dl_run_curl HEAD-probes the upstream and
            # wipes the partial itself if ETag/Last-Modified/size have
            # drifted (see _dl_probe_and_reconcile in lib/download-log.sh),
            # so leaving the file in place is safe. Previously this
            # branch tripped `read -r` on closed stdin which, under
            # `set -e`, killed the script with exit 1 before curl ran
            # — so the library's "Resume" button did nothing.
            echo "[*] Existing file detected; resuming via curl -C -"
        fi
    fi

    install_pmtiles
    mkdir -p "$TILES_DIR"

    if [ "$REGION" = "planet" ]; then
        echo "Downloading full planet file (this will take a while)..."
        dl_state_add "$TILES_DIR/$FILENAME" "$(dl_parse_size "$SIZE")"
        dl_run_curl "$TILES_DIR/$FILENAME" "$PLANET_URL" --progress-bar
    else
        # Regional extract via HTTP range requests — pmtiles extract
        # does its own network I/O so we can't track precise byte
        # progress, but we declare the target anyway for completeness.
        dl_state_add "$TILES_DIR/$FILENAME" "$(dl_parse_size "$SIZE")"
        echo "Extracting $DESC region via HTTP range requests..."
        echo "Bounding box: $BBOX"
        echo ""
        "$PMTILES_BIN" extract "$PLANET_URL" "$TILES_DIR/$FILENAME" --bbox="$BBOX"
    fi

    echo ""
    echo "Download complete: $TILES_DIR/$FILENAME"
}

download_terrain() {
    local REGION="$1"
    mkdir -p "$TILES_DIR"

    if [ "$REGION" = "global" ]; then
        local FILENAME="terrain-global.pmtiles"
        echo "Downloading global terrain data (z0-9, ~700 MB)..."
        echo "Destination: $TILES_DIR/$FILENAME"
        echo ""

        if [ -f "$TILES_DIR/$FILENAME" ]; then
            if [ ! -t 0 ]; then
                # Non-interactive: resume the partial instead of bailing.
                # See the matching comment in download_base_region().
                echo "[*] Existing file detected; resuming via curl -C -"
            else
                echo "File already exists. Re-download? (y/n)"
                read -r REPLY
                if [ "$REPLY" != "y" ]; then
                    echo "Cancelled."
                    return 0
                fi
                rm "$TILES_DIR/$FILENAME"
            fi
        fi

        dl_state_add "$TILES_DIR/$FILENAME" 734003200  # ~700 MB
        dl_run_curl "$TILES_DIR/$FILENAME" "$TERRAIN_GLOBAL_URL" --progress-bar
        echo ""
        echo "Download complete: $TILES_DIR/$FILENAME"
    else
        # Regional high-detail terrain — extract from global or build from AWS tiles
        local BBOX=""
        for entry in "${CATALOG[@]}"; do
            IFS='|' read -r key desc size bbox <<< "$entry"
            if [ "$key" = "$REGION" ]; then
                BBOX="$bbox"
                break
            fi
        done

        if [ -z "$BBOX" ]; then
            echo "Unknown region for terrain: $REGION"
            echo "Use 'global' for worldwide terrain, or a base map region name."
            exit 1
        fi

        echo "High-detail regional terrain (z0-12) for $REGION"
        echo "Note: This requires the global terrain file as a source."
        echo "If not yet downloaded, run: $0 --terrain global"
        echo ""

        if [ ! -f "$TILES_DIR/terrain-global.pmtiles" ]; then
            echo "Global terrain file not found. Downloading it first..."
            download_terrain "global"
            echo ""
        fi

        local FILENAME="terrain-${REGION}.pmtiles"
        install_pmtiles
        echo "Extracting terrain for $REGION..."
        "$PMTILES_BIN" extract "$TILES_DIR/terrain-global.pmtiles" "$TILES_DIR/$FILENAME" --bbox="$BBOX"
        echo ""
        echo "Download complete: $TILES_DIR/$FILENAME"
    fi
}

download_nautical() {
    local REGION="$1"
    local DESC="" SIZE="" SECTIONS=""

    # Check user-friendly region aliases first
    for entry in "${NAUTICAL_CATALOG[@]}"; do
        IFS='|' read -r key desc size sections <<< "$entry"
        if [ "$key" = "$REGION" ]; then
            DESC="$desc"; SIZE="$size"; SECTIONS="$sections"
            break
        fi
    done

    # Also allow direct NCDS section IDs (e.g., ncds_20a)
    if [ -z "$DESC" ]; then
        for entry in "${NCDS_SECTIONS[@]}"; do
            IFS='|' read -r id desc size_mb bbox <<< "$entry"
            if [ "$id" = "$REGION" ]; then
                DESC="$desc"; SIZE="~${size_mb} MB"; SECTIONS="$id"
                break
            fi
        done
    fi

    if [ -z "$DESC" ]; then
        echo "Unknown nautical region: $REGION"
        echo ""
        echo "Available nautical regions:"
        for entry in "${NAUTICAL_CATALOG[@]}"; do
            IFS='|' read -r key desc size sections <<< "$entry"
            printf "  %-20s %s (%s)\n" "$key" "$desc" "$size"
        done
        echo ""
        echo "Or use a direct NCDS section ID (e.g., ncds_20a)."
        echo "Run '$0 list' to see all sections."
        exit 1
    fi

    echo "Downloading NOAA nautical charts: $DESC ($SIZE)"
    echo "Source: NOAA Chart Display Service (NCDS)"
    echo "Sections: $SECTIONS"
    echo ""

    mkdir -p "$TILES_DIR"

    local SECTION_COUNT=0
    local TOTAL_SIZE=0

    for section in $SECTIONS; do
        local section_size_mb=""
        # Look up section info
        for entry in "${NCDS_SECTIONS[@]}"; do
            IFS='|' read -r id desc size_mb bbox <<< "$entry"
            if [ "$id" = "$section" ]; then
                section_size_mb="$size_mb"
                break
            fi
        done

        if [ -z "$section_size_mb" ]; then
            echo "Warning: Unknown NCDS section $section, skipping."
            continue
        fi

        local DEST="$TILES_DIR/nautical-${section}.mbtiles"
        local URL="${NCDS_BASE_URL}/${section}.mbtiles"

        if [ -f "$DEST" ]; then
            echo "  ✓ $section already downloaded ($(du -h "$DEST" | cut -f1))"
            SECTION_COUNT=$((SECTION_COUNT + 1))
            continue
        fi

        dl_state_add "$DEST" "$((section_size_mb * 1024 * 1024))"
        echo "  Downloading $section (~${section_size_mb} MB)..."
        if dl_run_curl "$DEST.tmp" "$URL" --progress-bar; then
            mv "$DEST.tmp" "$DEST"
            local actual_size
            actual_size="$(du -h "$DEST" | cut -f1)"
            echo "  ✓ $section complete ($actual_size)"
            SECTION_COUNT=$((SECTION_COUNT + 1))
        else
            rm -f "$DEST.tmp"
            echo "  ✗ Failed to download $section"
            echo "    URL: $URL"
            echo "    This section may be temporarily unavailable. Try again later."
        fi

        echo ""
    done

    echo "Download complete: $SECTION_COUNT section(s) saved to $TILES_DIR/"
    echo ""
    echo "NOAA charts include bathymetry, shipping lanes, buoys, navigational aids,"
    echo "depth soundings, and more. Charts are updated weekly by NOAA."
}

download_aeronautical() {
    local REGION="$1"
    local DESC="" SIZE="" CHARTS=""

    # Check user-friendly region aliases first
    for entry in "${AERO_CATALOG[@]}"; do
        IFS='|' read -r key desc size charts <<< "$entry"
        if [ "$key" = "$REGION" ]; then
            DESC="$desc"; SIZE="$size"; CHARTS="$charts"
            break
        fi
    done

    # Also allow direct FAA chart names (e.g., Seattle)
    if [ -z "$DESC" ]; then
        for entry in "${FAA_SECTIONS[@]}"; do
            IFS='|' read -r name desc size_mb bbox <<< "$entry"
            if [ "$name" = "$REGION" ]; then
                DESC="$desc"; SIZE="~${size_mb} MB"; CHARTS="$name"
                break
            fi
        done
    fi

    if [ -z "$DESC" ]; then
        echo "Unknown aeronautical region: $REGION"
        echo ""
        echo "Available aeronautical regions:"
        for entry in "${AERO_CATALOG[@]}"; do
            IFS='|' read -r key desc size charts <<< "$entry"
            printf "  %-20s %s (%s)\n" "$key" "$desc" "$size"
        done
        echo ""
        echo "Or use a direct FAA chart name (e.g., Seattle, Klamath_Falls)."
        exit 1
    fi

    echo "Downloading FAA VFR Sectional Charts: $DESC ($SIZE)"
    echo "Source: FAA Aeronautical Navigation Products"
    echo "Edition: $FAA_EDITION"
    echo "Charts: $CHARTS"
    echo ""

    # Check for GDAL (needed for GeoTIFF → MBTiles conversion)
    if ! docker image inspect ghcr.io/osgeo/gdal:alpine-small-latest >/dev/null 2>&1; then
        echo "Pulling GDAL Docker image for chart conversion..."
        docker pull ghcr.io/osgeo/gdal:alpine-small-latest
        echo ""
    fi

    local WORK_DIR="$PROJECT_DIR/data/aero-work"
    mkdir -p "$WORK_DIR" "$TILES_DIR"

    for chart in $CHARTS; do
        local DEST="$TILES_DIR/aero-${chart}.mbtiles"

        if [ -f "$DEST" ]; then
            echo "  ✓ $chart already converted ($(du -h "$DEST" | cut -f1))"
            continue
        fi

        local ZIP_FILE="$WORK_DIR/${chart}.zip"
        local URL="${FAA_BASE_URL}/${chart}.zip"

        # Download
        if [ ! -f "$ZIP_FILE" ]; then
            echo "  Downloading $chart..."
            dl_state_add "$ZIP_FILE" 0  # FAA zips vary; no upfront size
            if ! dl_run_curl "$ZIP_FILE.tmp" "$URL" --progress-bar; then
                rm -f "$ZIP_FILE.tmp"
                echo "  ✗ Failed to download $chart"
                echo "    URL: $URL"
                echo "    The FAA edition date may have changed. Set FAA_EDITION env var."
                continue
            fi
            mv "$ZIP_FILE.tmp" "$ZIP_FILE"
        fi

        # Extract GeoTIFF
        echo "  Extracting $chart..."
        unzip -qo "$ZIP_FILE" -d "$WORK_DIR/${chart}"

        # Find the .tif file
        local TIF_FILE
        TIF_FILE=$(find "$WORK_DIR/${chart}" -name "*.tif" -type f | head -1)
        if [ -z "$TIF_FILE" ]; then
            echo "  ✗ No GeoTIFF found in $chart.zip"
            continue
        fi

        # Convert GeoTIFF → MBTiles using GDAL in Docker
        echo "  Converting $chart to MBTiles (this takes a few minutes)..."
        docker run --rm \
            -v "$WORK_DIR:/work" \
            -v "$TILES_DIR:/out" \
            ghcr.io/osgeo/gdal:alpine-small-latest \
            sh -c "
                cd /work/${chart}
                TIF=\$(find . -name '*.tif' | head -1)
                # Expand color palette to full RGBA (required for correct resampling)
                gdal_translate -expand rgba \"\$TIF\" /work/${chart}_rgba.tif
                # Reproject to Web Mercator
                gdalwarp -t_srs EPSG:3857 -r bilinear \
                    -co COMPRESS=DEFLATE \
                    /work/${chart}_rgba.tif /work/${chart}_warped.tif
                # Convert directly to MBTiles
                gdal_translate -of MBTiles -co TILE_FORMAT=PNG \
                    /work/${chart}_warped.tif /out/aero-${chart}.mbtiles
                # Build overview zoom levels for fast rendering
                gdaladdo -r bilinear /out/aero-${chart}.mbtiles 2 4 8 16 32 64 128 256
                # Clean up intermediate files
                rm -f /work/${chart}_rgba.tif /work/${chart}_warped.tif
            "

        if [ -f "$DEST" ]; then
            local actual_size
            actual_size="$(du -h "$DEST" | cut -f1)"
            echo "  ✓ $chart converted ($actual_size)"
        else
            echo "  ✗ Conversion failed for $chart"
        fi

        # Clean up intermediate files
        rm -rf "$WORK_DIR/${chart}" "$WORK_DIR/${chart}_warped.tif" "$WORK_DIR/${chart}_tiles"

        echo ""
    done

    echo "Download complete. FAA VFR charts show terrain, airports, airspace,"
    echo "navigation aids, obstacles, and more. Updated every 56 days."
    echo ""
    echo "To update when a new edition releases, set FAA_EDITION and re-run."
}

cmd_list() {
    echo "Base map regions:"
    echo ""
    for entry in "${CATALOG[@]}"; do
        IFS='|' read -r key desc size bbox <<< "$entry"
        local status="  "
        [ -f "$TILES_DIR/${key}.pmtiles" ] && status="✓ "
        printf "  %s%-20s %s (%s)\n" "$status" "$key" "$desc" "$size"
    done
    echo ""
    echo "Terrain data:"
    echo ""
    local status="  "
    [ -f "$TILES_DIR/terrain-global.pmtiles" ] && status="✓ "
    printf "  %s%-20s %s\n" "$status" "global" "Global terrain z0-9 (~700 MB)"
    echo ""
    echo "Nautical regions (NOAA charts):"
    echo ""
    for entry in "${NAUTICAL_CATALOG[@]}"; do
        IFS='|' read -r key desc size sections <<< "$entry"
        # Check if all sections for this region are downloaded
        local all_present=true
        for section in $sections; do
            if [ ! -f "$TILES_DIR/nautical-${section}.mbtiles" ]; then
                all_present=false
                break
            fi
        done
        local status="  "
        $all_present && status="✓ "
        printf "  %s%-20s %s (%s)\n" "$status" "$key" "$desc" "$size"
    done
    echo ""
    echo "Aeronautical regions (FAA VFR charts, edition $FAA_EDITION):"
    echo ""
    for entry in "${AERO_CATALOG[@]}"; do
        IFS='|' read -r key desc size charts <<< "$entry"
        local all_present=true
        for chart in $charts; do
            if [ ! -f "$TILES_DIR/aero-${chart}.mbtiles" ]; then
                all_present=false
                break
            fi
        done
        local status="  "
        $all_present && status="✓ "
        printf "  %s%-20s %s (%s)\n" "$status" "$key" "$desc" "$size"
    done
    echo ""
    echo "✓ = downloaded"
}

cmd_status() {
    echo "Downloaded map tiles:"
    echo ""
    local total=0
    if ls "$TILES_DIR"/*.pmtiles "$TILES_DIR"/*.mbtiles 1>/dev/null 2>&1; then
        for f in "$TILES_DIR"/*.pmtiles "$TILES_DIR"/*.mbtiles; do
            [ -f "$f" ] || continue
            local name
            name="$(basename "$f")"
            local size
            size="$(du -h "$f" | cut -f1)"
            local bytes
            bytes="$(stat -c%s "$f" 2>/dev/null || stat -f%z "$f" 2>/dev/null)"
            total=$((total + bytes))
            printf "  %-40s %s\n" "$name" "$size"
        done
        echo ""
        local total_h
        total_h="$(echo "$total" | awk '{if($1>=1073741824) printf "%.1f GB", $1/1073741824; else if($1>=1048576) printf "%.1f MB", $1/1048576; else printf "%.1f KB", $1/1024}')"
        echo "  Total: $total_h"
    else
        echo "  No tiles downloaded yet."
        echo "  Run: $0 <region> to download base map tiles"
    fi
}

cmd_remove() {
    local TARGET="$1"
    if [ -z "$TARGET" ]; then
        echo "Usage: $0 remove <filename>"
        echo "Example: $0 remove oregon.pmtiles"
        echo ""
        echo "Use '$0 status' to see downloaded files."
        exit 1
    fi

    # Add .pmtiles extension if not present
    [[ "$TARGET" != *.pmtiles ]] && TARGET="${TARGET}.pmtiles"

    local FILEPATH="$TILES_DIR/$TARGET"
    if [ ! -f "$FILEPATH" ]; then
        echo "File not found: $FILEPATH"
        exit 1
    fi

    local SIZE
    SIZE="$(du -h "$FILEPATH" | cut -f1)"
    echo "Remove $TARGET ($SIZE)? (y/n)"
    read -r REPLY
    if [ "$REPLY" = "y" ]; then
        rm "$FILEPATH"
        echo "Removed: $TARGET"
        echo ""
        echo "Restart the tile server to apply: docker restart krull-tileserver"
    else
        echo "Cancelled."
    fi
}

# Parse arguments
if [ $# -eq 0 ]; then
    print_usage
    exit 1
fi

# For any command that actually downloads data, open a state entry and
# install an exit trap so the active slot is always cleared — even on
# Ctrl-C or set -e aborts. The trap captures $? so the terminal status
# reflects the actual exit code. list/status/remove bypass this.
_dl_on_exit() {
    local ec=$?
    if [ -n "${DL_CURRENT_KEY:-}" ]; then
        if [ "$ec" -ne 0 ]; then
            dl_state_end failed || true
        else
            dl_state_end done || true
        fi
    fi
}
case "$1" in
    list|status|remove)
        ;;
    --base-only|--terrain|--nautical|--aeronautical|--aero|--all)
        trap _dl_on_exit EXIT
        dl_state_begin maps "${2:-unknown}" "Maps: ${2:-unknown} ($1)"
        ;;
    *)
        trap _dl_on_exit EXIT
        dl_state_begin maps "$1" "Maps: $1 (full region)"
        ;;
esac

case "$1" in
    list)
        cmd_list
        ;;
    status)
        cmd_status
        ;;
    remove)
        cmd_remove "$2"
        ;;
    --base-only)
        if [ -z "$2" ]; then
            echo "Usage: $0 --base-only <region>"
            exit 1
        fi
        download_base_tiles "$2"
        echo ""
        echo "Restart the tile server to load it: docker restart krull-tileserver"
        ;;
    --terrain)
        if [ -z "$2" ]; then
            echo "Usage: $0 --terrain <global|region>"
            exit 1
        fi
        download_terrain "$2"
        echo ""
        echo "Restart the tile server to load it: docker restart krull-tileserver"
        ;;
    --nautical)
        if [ -z "$2" ]; then
            echo "Usage: $0 --nautical <region>"
            echo "Run '$0 list' to see available nautical regions."
            exit 1
        fi
        download_nautical "$2"
        echo ""
        echo "Restart the tile server to load it: docker restart krull-tileserver"
        ;;
    --aeronautical|--aero)
        if [ -z "$2" ]; then
            echo "Usage: $0 --aeronautical <region>"
            echo "Run '$0 list' to see available aeronautical regions."
            exit 1
        fi
        download_aeronautical "$2"
        echo ""
        echo "Restart the tile server to load it: docker restart krull-tileserver"
        ;;
    --all)
        if [ -z "$2" ]; then
            echo "Usage: $0 --all <region>"
            exit 1
        fi
        download_base_tiles "$2"
        echo ""
        echo "---"
        echo ""
        download_terrain "global"
        echo ""
        echo "---"
        echo ""
        # Auto-detect overlapping NOAA nautical sections for this region's bbox
        local REGION_BBOX=""
        for entry in "${CATALOG[@]}"; do
            IFS='|' read -r key desc size bbox <<< "$entry"
            if [ "$key" = "$2" ]; then
                REGION_BBOX="$bbox"
                break
            fi
        done
        if [ -n "$REGION_BBOX" ]; then
            local matching_sections=""
            IFS=',' read -r rW rS rE rN <<< "$REGION_BBOX"
            for entry in "${NCDS_SECTIONS[@]}"; do
                IFS='|' read -r id desc size_mb bbox <<< "$entry"
                IFS=',' read -r sW sS sE sN <<< "$bbox"
                # Check bounding box overlap using awk for float comparison
                if echo "$rW $rE $sW $sE $rS $rN $sS $sN" | awk '{
                    if ($1 < $4 && $2 > $3 && $5 < $8 && $6 > $7) exit 0; else exit 1
                }'; then
                    matching_sections="$matching_sections $id"
                fi
            done
            if [ -n "$matching_sections" ]; then
                echo "Auto-detected NOAA nautical chart sections overlapping $2:"
                echo " $matching_sections"
                echo ""
                # Download each matching section directly
                mkdir -p "$TILES_DIR"
                for section in $matching_sections; do
                    local section_size_mb=""
                    for entry in "${NCDS_SECTIONS[@]}"; do
                        IFS='|' read -r id desc size_mb bbox <<< "$entry"
                        if [ "$id" = "$section" ]; then
                            section_size_mb="$size_mb"
                            break
                        fi
                    done
                    local DEST="$TILES_DIR/nautical-${section}.mbtiles"
                    if [ -f "$DEST" ]; then
                        echo "  ✓ $section already downloaded"
                        continue
                    fi
                    echo "  Downloading $section (~${section_size_mb} MB)..."
                    local URL="${NCDS_BASE_URL}/${section}.mbtiles"
                    dl_state_add "$DEST" "$((section_size_mb * 1024 * 1024))"
                    if dl_run_curl "$DEST.tmp" "$URL" --progress-bar; then
                        mv "$DEST.tmp" "$DEST"
                        echo "  ✓ $section complete"
                    else
                        rm -f "$DEST.tmp"
                        echo "  ✗ Failed to download $section"
                    fi
                done
            else
                echo "No NOAA nautical charts available for this region (inland area)."
            fi

            echo ""
            echo "---"
            echo ""

            # Auto-detect overlapping FAA VFR sectional charts
            local matching_charts=""
            for entry in "${FAA_SECTIONS[@]}"; do
                IFS='|' read -r name desc size_mb bbox <<< "$entry"
                IFS=',' read -r sW sS sE sN <<< "$bbox"
                if echo "$rW $rE $sW $sE $rS $rN $sS $sN" | awk '{
                    if ($1 < $4 && $2 > $3 && $5 < $8 && $6 > $7) exit 0; else exit 1
                }'; then
                    matching_charts="$matching_charts $name"
                fi
            done
            if [ -n "$matching_charts" ]; then
                echo "Auto-detected FAA VFR charts overlapping $2:"
                echo " $matching_charts"
                echo ""
                for achart in $matching_charts; do
                    download_aeronautical "$achart"
                done
            fi
        fi
        echo ""
        echo "Restart the tile server to load everything: docker restart krull-tileserver"
        ;;
    *)
        # Default: download everything for the region (same as --all)
        download_base_tiles "$1"
        echo ""
        echo "---"
        echo ""
        download_terrain "global"
        echo ""
        echo "---"
        echo ""
        # Auto-detect overlapping NOAA nautical sections
        local REGION_BBOX=""
        for entry in "${CATALOG[@]}"; do
            IFS='|' read -r key desc size bbox <<< "$entry"
            if [ "$key" = "$1" ]; then
                REGION_BBOX="$bbox"
                break
            fi
        done
        if [ -n "$REGION_BBOX" ]; then
            local matching_sections=""
            IFS=',' read -r rW rS rE rN <<< "$REGION_BBOX"
            for entry in "${NCDS_SECTIONS[@]}"; do
                IFS='|' read -r id desc size_mb bbox <<< "$entry"
                IFS=',' read -r sW sS sE sN <<< "$bbox"
                if echo "$rW $rE $sW $sE $rS $rN $sS $sN" | awk '{
                    if ($1 < $4 && $2 > $3 && $5 < $8 && $6 > $7) exit 0; else exit 1
                }'; then
                    matching_sections="$matching_sections $id"
                fi
            done
            if [ -n "$matching_sections" ]; then
                echo "Auto-detected NOAA nautical charts overlapping $1:"
                echo " $matching_sections"
                echo ""
                mkdir -p "$TILES_DIR"
                for section in $matching_sections; do
                    local section_size_mb=""
                    for entry in "${NCDS_SECTIONS[@]}"; do
                        IFS='|' read -r id desc size_mb bbox <<< "$entry"
                        if [ "$id" = "$section" ]; then
                            section_size_mb="$size_mb"
                            break
                        fi
                    done
                    local DEST="$TILES_DIR/nautical-${section}.mbtiles"
                    if [ -f "$DEST" ]; then
                        echo "  ✓ $section already downloaded"
                        continue
                    fi
                    echo "  Downloading $section (~${section_size_mb} MB)..."
                    local URL="${NCDS_BASE_URL}/${section}.mbtiles"
                    dl_state_add "$DEST" "$((section_size_mb * 1024 * 1024))"
                    if dl_run_curl "$DEST.tmp" "$URL" --progress-bar; then
                        mv "$DEST.tmp" "$DEST"
                        echo "  ✓ $section complete"
                    else
                        rm -f "$DEST.tmp"
                        echo "  ✗ Failed to download $section"
                    fi
                done
            fi

            echo ""
            echo "---"
            echo ""

            # Auto-detect overlapping FAA VFR charts
            local matching_charts=""
            for entry in "${FAA_SECTIONS[@]}"; do
                IFS='|' read -r name desc size_mb bbox <<< "$entry"
                IFS=',' read -r sW sS sE sN <<< "$bbox"
                if echo "$rW $rE $sW $sE $rS $rN $sS $sN" | awk '{
                    if ($1 < $4 && $2 > $3 && $5 < $8 && $6 > $7) exit 0; else exit 1
                }'; then
                    matching_charts="$matching_charts $name"
                fi
            done
            if [ -n "$matching_charts" ]; then
                echo "Auto-detected FAA VFR charts overlapping $1:"
                echo " $matching_charts"
                echo ""
                for achart in $matching_charts; do
                    download_aeronautical "$achart"
                done
            fi
        fi
        echo ""
        echo "Restart the tile server to load everything: docker restart krull-tileserver"
        ;;
esac
