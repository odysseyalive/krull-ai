#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
ZIM_DIR="$PROJECT_DIR/zim"

echo "Knowledge Base Downloader for Krull AI"
echo ""

# --- Catalog ---
# Each entry: KEY|FILE|DESCRIPTION|SIZE
CATALOG=(
    # Developer Documentation (DevDocs)
    "devdocs-python|devdocs/devdocs_en_python_2026-02.zim|Python standard library docs|4 MB"
    "devdocs-javascript|devdocs/devdocs_en_javascript_2026-01.zim|JavaScript reference|3 MB"
    "devdocs-typescript|devdocs/devdocs_en_typescript_2026-01.zim|TypeScript reference|3 MB"
    "devdocs-node|devdocs/devdocs_en_node_2026-01.zim|Node.js docs|5 MB"
    "devdocs-react|devdocs/devdocs_en_react_2026-01.zim|React docs|2 MB"
    "devdocs-docker|devdocs/devdocs_en_docker_2026-01.zim|Docker documentation|2 MB"
    "devdocs-kubernetes|devdocs/devdocs_en_kubernetes_2026-01.zim|Kubernetes docs|1 MB"
    "devdocs-git|devdocs/devdocs_en_git_2026-01.zim|Git reference|2 MB"
    "devdocs-rust|devdocs/devdocs_en_rust_2026-01.zim|Rust documentation|8 MB"
    "devdocs-go|devdocs/devdocs_en_go_2026-01.zim|Go documentation|4 MB"
    "devdocs-bash|devdocs/devdocs_en_bash_2026-01.zim|Bash reference|1 MB"
    "devdocs-css|devdocs/devdocs_en_css_2026-01.zim|CSS reference|8 MB"
    "devdocs-html|devdocs/devdocs_en_html_2026-01.zim|HTML reference|3 MB"
    "devdocs-sql|devdocs/devdocs_en_postgresql_2026-01.zim|PostgreSQL docs|15 MB"
    "devdocs-numpy|devdocs/devdocs_en_numpy_2026-01.zim|NumPy docs|5 MB"
    "devdocs-pandas|devdocs/devdocs_en_pandas_2026-01.zim|Pandas docs|12 MB"
    "devdocs-scikit|devdocs/devdocs_en_scikit-learn_2026-01.zim|Scikit-learn docs|54 MB"

    # Stack Exchange
    "stackexchange-unix|stack_exchange/unix.stackexchange.com_en_all_2026-02.zim|Unix & Linux Q&A|1.2 GB"
    "stackexchange-ubuntu|stack_exchange/askubuntu.com_en_all_2025-12.zim|Ask Ubuntu Q&A|2.6 GB"
    "stackexchange-codereview|stack_exchange/codereview.stackexchange.com_en_all_2026-02.zim|Code Review Q&A|525 MB"
    "stackexchange-security|stack_exchange/security.stackexchange.com_en_all_2026-02.zim|Information Security Q&A|420 MB"
    "stackexchange-serverfault|stack_exchange/serverfault.com_en_all_2026-02.zim|Server administration Q&A|1.5 GB"
    "stackexchange-superuser|stack_exchange/superuser.com_en_all_2026-02.zim|Computer hardware & software Q&A|3.7 GB"
    "stackexchange-softeng|stack_exchange/softwareengineering.stackexchange.com_en_all_2026-02.zim|Software engineering Q&A|457 MB"
    "stackoverflow|stack_exchange/stackoverflow.com_en_all_2023-11.zim|Full Stack Overflow archive|75 GB"

    # Linux
    "archlinux|other/archlinux_en_all_maxi_2025-09.zim|Arch Linux Wiki|30 MB"

    # Reference
    "wiktionary|wiktionary/wiktionary_en_all_nopic_2026-02.zim|English dictionary & thesaurus|8.2 GB"

    # Books
    "gutenberg|gutenberg/gutenberg_en_all_2025-11.zim|Project Gutenberg — all English books|206 GB"
)

# --- Bundles ---
print_bundles() {
    echo "Bundles (download multiple packages at once):"
    echo ""
    echo "  dev-essentials    Core developer docs (~50 MB)"
    echo "                    python, javascript, typescript, node, git, docker, bash"
    echo ""
    echo "  web-dev           Web development stack (~25 MB)"
    echo "                    javascript, typescript, react, css, html, node"
    echo ""
    echo "  data-science      Data science & ML (~75 MB)"
    echo "                    python, numpy, pandas, scikit"
    echo ""
    echo "  sysadmin          System administration (~5.5 GB)"
    echo "                    archlinux, stackexchange-unix, stackexchange-serverfault"
    echo ""
    echo "  community         Developer Q&A (~5 GB)"
    echo "                    stackexchange-unix, stackexchange-codereview,"
    echo "                    stackexchange-security, stackexchange-softeng"
    echo ""
}

get_bundle_keys() {
    case "$1" in
        dev-essentials)
            echo "devdocs-python devdocs-javascript devdocs-typescript devdocs-node devdocs-git devdocs-docker devdocs-bash"
            ;;
        web-dev)
            echo "devdocs-javascript devdocs-typescript devdocs-react devdocs-css devdocs-html devdocs-node"
            ;;
        data-science)
            echo "devdocs-python devdocs-numpy devdocs-pandas devdocs-scikit"
            ;;
        sysadmin)
            echo "archlinux stackexchange-unix stackexchange-serverfault"
            ;;
        community)
            echo "stackexchange-unix stackexchange-codereview stackexchange-security stackexchange-softeng"
            ;;
        *)
            echo ""
            ;;
    esac
}

# --- Functions ---
print_usage() {
    echo "Usage: $0 <package|bundle> [package2 ...]"
    echo ""
    echo "Packages:"
    echo ""
    echo "  Developer Documentation:"
    for entry in "${CATALOG[@]}"; do
        IFS='|' read -r key file desc size <<< "$entry"
        case "$key" in devdocs-*)
            printf "    %-25s %s (%s)\n" "$key" "$desc" "$size"
            ;;
        esac
    done
    echo ""
    echo "  Stack Exchange:"
    for entry in "${CATALOG[@]}"; do
        IFS='|' read -r key file desc size <<< "$entry"
        case "$key" in stackexchange-*|stackoverflow)
            printf "    %-25s %s (%s)\n" "$key" "$desc" "$size"
            ;;
        esac
    done
    echo ""
    echo "  Linux:"
    for entry in "${CATALOG[@]}"; do
        IFS='|' read -r key file desc size <<< "$entry"
        case "$key" in archlinux)
            printf "    %-25s %s (%s)\n" "$key" "$desc" "$size"
            ;;
        esac
    done
    echo ""
    echo "  Reference & Books:"
    for entry in "${CATALOG[@]}"; do
        IFS='|' read -r key file desc size <<< "$entry"
        case "$key" in wiktionary|gutenberg)
            printf "    %-25s %s (%s)\n" "$key" "$desc" "$size"
            ;;
        esac
    done
    echo ""
    print_bundles
    echo "Examples:"
    echo "  $0 devdocs-python devdocs-git       # Download two packages"
    echo "  $0 dev-essentials                    # Download the developer essentials bundle"
    echo "  $0 archlinux stackexchange-unix      # Download Arch Wiki + Unix Q&A"
    echo ""
    echo "After downloading, restart Kiwix: docker restart krull-kiwix"
}

download_package() {
    local key="$1"
    local file=""
    local desc=""
    local size=""

    for entry in "${CATALOG[@]}"; do
        IFS='|' read -r k f d s <<< "$entry"
        if [ "$k" = "$key" ]; then
            file="$f"
            desc="$d"
            size="$s"
            break
        fi
    done

    if [ -z "$file" ]; then
        echo "[-] Unknown package: $key"
        return 1
    fi

    local filename
    filename=$(basename "$file")

    if [ -f "$ZIM_DIR/$filename" ]; then
        echo "[+] Already downloaded: $desc ($filename)"
        return 0
    fi

    echo "[*] Downloading: $desc ($size)"
    echo "    File: $filename"

    curl -L -C - -o "$ZIM_DIR/$filename" \
        "https://download.kiwix.org/zim/$file" \
        --progress-bar

    echo "[+] Done: $filename"
    echo ""
}

# --- Main ---
if [ $# -eq 0 ]; then
    print_usage
    exit 1
fi

PACKAGES=""

for arg in "$@"; do
    # Check if it's a bundle
    bundle_keys=$(get_bundle_keys "$arg")
    if [ -n "$bundle_keys" ]; then
        PACKAGES="$PACKAGES $bundle_keys"
    else
        PACKAGES="$PACKAGES $arg"
    fi
done

# Remove duplicates while preserving order
PACKAGES=$(echo "$PACKAGES" | tr ' ' '\n' | awk '!seen[$0]++' | tr '\n' ' ')

FAIL=0
for pkg in $PACKAGES; do
    download_package "$pkg" || FAIL=1
done

if [ "$FAIL" -eq 0 ]; then
    echo ""
    echo "All downloads complete. Restart Kiwix to load them:"
    echo "  docker restart krull-kiwix"
fi
