#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

echo "Starting Krull AI..."
echo ""

# --- Detect OS/distro for install hints ---
detect_distro() {
    if [ -f /etc/os-release ]; then
        . /etc/os-release
        echo "$ID"
    elif command -v sw_vers &> /dev/null; then
        echo "macos"
    else
        echo "unknown"
    fi
}

DISTRO=$(detect_distro)
IS_MACOS=false
[[ "$OSTYPE" == "darwin"* ]] && IS_MACOS=true

# Cross-platform sed -i (GNU vs BSD)
sedi() {
    if $IS_MACOS; then
        sed -i '' "$@"
    else
        sed -i "$@"
    fi
}

install_hint() {
    local pkg="$1"
    case "$DISTRO" in
        arch|manjaro|endeavouros|garuda)
            case "$pkg" in
                docker)         echo "  sudo pacman -S docker" ;;
                docker-compose) echo "  sudo pacman -S docker-compose" ;;
                curl)           echo "  sudo pacman -S curl" ;;
                nvidia-container-toolkit)
                    echo "  yay -S nvidia-container-toolkit  (AUR)" ;;
            esac
            ;;
        ubuntu|debian|pop|linuxmint)
            case "$pkg" in
                docker)         echo "  sudo apt install docker.io" ;;
                docker-compose) echo "  sudo apt install docker-compose-v2" ;;
                curl)           echo "  sudo apt install curl" ;;
                nvidia-container-toolkit)
                    echo "  See: https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html"
                    echo "  sudo apt install nvidia-container-toolkit" ;;
            esac
            ;;
        fedora|rhel|centos|rocky|alma)
            case "$pkg" in
                docker)         echo "  sudo dnf install docker-ce  (or: sudo dnf install podman-docker)" ;;
                docker-compose) echo "  sudo dnf install docker-compose-plugin" ;;
                curl)           echo "  sudo dnf install curl" ;;
                nvidia-container-toolkit)
                    echo "  See: https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html"
                    echo "  sudo dnf install nvidia-container-toolkit" ;;
            esac
            ;;
        opensuse*|sles)
            case "$pkg" in
                docker)         echo "  sudo zypper install docker" ;;
                docker-compose) echo "  sudo zypper install docker-compose" ;;
                curl)           echo "  sudo zypper install curl" ;;
                nvidia-container-toolkit)
                    echo "  See: https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html" ;;
            esac
            ;;
        macos)
            case "$pkg" in
                docker)         echo "  brew install --cask docker  (or install Docker Desktop)" ;;
                docker-compose) echo "  Docker Compose is included with Docker Desktop" ;;
                curl)           echo "  curl is pre-installed on macOS" ;;
                ollama)         echo "  brew install ollama" ;;
                nvidia-container-toolkit)
                    echo "  Not applicable on macOS. Native Ollama uses Metal for GPU acceleration." ;;
            esac
            ;;
        *)
            echo "  Please install '$pkg' using your system's package manager."
            ;;
    esac
}

# --- Dependency checks ---
MISSING=0

# Check: docker
if ! command -v docker &> /dev/null; then
    echo "[-] MISSING: docker"
    install_hint docker
    MISSING=1
else
    echo "[+] docker found"
fi

# Check: docker compose (v2 plugin)
if ! docker compose version &> /dev/null; then
    echo "[-] MISSING: docker compose (v2 plugin)"
    install_hint docker-compose
    MISSING=1
else
    echo "[+] docker compose found"
fi

# Check: curl (needed by download-wikipedia.sh)
if ! command -v curl &> /dev/null; then
    echo "[-] MISSING: curl"
    install_hint curl
    MISSING=1
else
    echo "[+] curl found"
fi

if [ "$MISSING" -ne 0 ]; then
    echo ""
    echo "ERROR: Install the missing packages above, then re-run this script."
    exit 1
fi

echo ""

# Check: Docker daemon running
if ! docker info &> /dev/null; then
    echo "ERROR: Docker daemon is not running."
    case "$DISTRO" in
        arch|manjaro|endeavouros|garuda|ubuntu|debian|pop|linuxmint|fedora|rhel|centos|rocky|alma|opensuse*)
            echo "  sudo systemctl start docker" ;;
        macos)
            echo "  Open the Docker Desktop application." ;;
    esac
    exit 1
fi

# --- .env file management ---
# Copy .env.sample as starting point if .env doesn't exist
ENV_FILE="$PROJECT_DIR/.env"
if [ ! -f "$ENV_FILE" ]; then
    if [ -f "$PROJECT_DIR/.env.sample" ]; then
        cp "$PROJECT_DIR/.env.sample" "$ENV_FILE"
        echo "[+] Created .env from .env.sample"
    else
        cat > "$ENV_FILE" << 'ENV_EOF'
OLLAMA_MODEL=frob/qwen3.5-instruct:9b
OLLAMA_NUM_CTX=131072
ENV_EOF
    fi
fi

# Ensure all canonical .env defaults are present so the Settings page in
# Krull Home shows every known field pre-populated. Source of truth is
# krull-home/server/lib/envSchema.ts — keep these in sync.
# Idempotent: only appends missing keys; never overwrites user-set values.
ensure_env_default() {
    local key="$1"
    local value="$2"
    grep -q "^${key}=" "$ENV_FILE" || echo "${key}=${value}" >> "$ENV_FILE"
}

ensure_env_default OLLAMA_MODEL            "frob/qwen3.5-instruct:9b"
ensure_env_default OLLAMA_NUM_CTX          "131072"
ensure_env_default OLLAMA_TEMPERATURE      "0.8"
ensure_env_default OLLAMA_TOP_P            "0.8"
ensure_env_default OLLAMA_TOP_K            "20"
ensure_env_default OLLAMA_PRESENCE_PENALTY "1.5"
ensure_env_default CONTEXT_COMPACT_LIMIT   "98304"
ensure_env_default WEBUI_SECRET_KEY        "changeme-generate-a-real-key"
ensure_env_default LITELLM_MASTER_KEY      "sk-local-dev-key"
ensure_env_default FAA_EDITION             "03-19-2026"
# PHOTON_COUNTRY_CODE intentionally not defaulted — empty means worldwide coverage.

# Source .env so we can use the values in this script
set -a
source "$ENV_FILE"
set +a

# Platform-aware compose file assembly
COMPOSE_FILES="-f $PROJECT_DIR/docker-compose.yml"

if $IS_MACOS; then
    # macOS: use native Ollama for Metal GPU acceleration (Docker has no GPU passthrough on macOS)
    COMPOSE_FILES="$COMPOSE_FILES -f $PROJECT_DIR/docker-compose.macos.yml"

    if curl -sf http://localhost:11434/api/tags &>/dev/null; then
        echo "[+] Native Ollama detected (Metal GPU acceleration)"
    else
        echo "[!] Ollama is not running on this Mac."
        install_hint ollama
        echo "    Start with: ollama serve"
        echo ""
        echo "    Continuing anyway — Ollama can be started later."
    fi

    sedi '/^COMPOSE_FILE=/d' "$ENV_FILE"
    echo "COMPOSE_FILE=docker-compose.yml:docker-compose.macos.yml" >> "$ENV_FILE"
else
    # Linux: add linux-specific mounts
    COMPOSE_FILES="$COMPOSE_FILES -f $PROJECT_DIR/docker-compose.linux.yml"

    # Check NVIDIA — verify both the container runtime AND that the driver is actually loaded
    if docker info 2>/dev/null | grep -qi nvidia && nvidia-smi &>/dev/null; then
        echo "[+] NVIDIA GPU available (driver loaded)"
        COMPOSE_FILES="$COMPOSE_FILES -f $PROJECT_DIR/docker-compose.gpu.yml"
        sedi '/^COMPOSE_FILE=/d' "$ENV_FILE"
        echo "COMPOSE_FILE=docker-compose.yml:docker-compose.linux.yml:docker-compose.gpu.yml" >> "$ENV_FILE"
    else
        echo "[!] NVIDIA GPU not available — Ollama will use CPU only."
        sedi '/^COMPOSE_FILE=/d' "$ENV_FILE"
        echo "COMPOSE_FILE=docker-compose.yml:docker-compose.linux.yml" >> "$ENV_FILE"
        if ! docker info 2>/dev/null | grep -qi nvidia; then
            install_hint nvidia-container-toolkit
        elif ! nvidia-smi &>/dev/null; then
            echo "    nvidia-container-toolkit is installed but the driver is not loaded."
            echo "    Check: sudo modprobe nvidia  or reboot after a kernel/driver update."
        fi
    fi
fi

# Check for ZIM files — auto-download mini if none exist
ZIM_COUNT=$(find "$PROJECT_DIR/zim" -name "*.zim" 2>/dev/null | wc -l)
if [ "$ZIM_COUNT" -eq 0 ]; then
    echo "[!] No ZIM files found. Downloading Wikipedia Mini (~5 MB)..."
    "$SCRIPT_DIR/download-wikipedia.sh" mini
    echo ""
    echo "    Upgrade later with: ./scripts/download-wikipedia.sh medicine|nopic|full"
else
    echo "[+] Found $ZIM_COUNT ZIM file(s) in zim/"
fi

echo ""

# start.sh never updates images. It only starts what's already been
# downloaded/built, plus brings up anything missing on first run
# (docker compose up -d builds missing build-services and pulls missing
# images automatically — but never touches existing ones).
# Use ./scripts/update.sh to pull the latest versions explicitly.
echo "[+] start.sh will not pull updates — use ./scripts/update.sh for that"

# Ensure bind-mount directories exist with correct ownership
mkdir -p "$PROJECT_DIR/data/ollama" "$PROJECT_DIR/data/webui" "$PROJECT_DIR/data/tiles" "$PROJECT_DIR/data/photon"
mkdir -p "$PROJECT_DIR/data/searxng"

# Write default SearXNG config if it doesn't exist
if [ ! -f "$PROJECT_DIR/data/searxng/settings.yml" ]; then
    cat > "$PROJECT_DIR/data/searxng/settings.yml" << 'SEARXNG_EOF'
use_default_settings: true

server:
  secret_key: "krull-searxng-secret-change-me"
  limiter: false
  image_proxy: false

search:
  safe_search: 0
  autocomplete: ""
  default_lang: "en"
  formats:
    - html
    - json

engines:
  - name: google
    engine: google
    shortcut: g
    disabled: false

  - name: duckduckgo
    engine: duckduckgo
    shortcut: ddg
    disabled: false

  - name: brave
    engine: brave
    shortcut: br
    disabled: false

  - name: wikipedia
    engine: wikipedia
    shortcut: wp
    disabled: false
SEARXNG_EOF
fi

# Update LiteLLM config with current model from .env
echo "[+] Model: $OLLAMA_MODEL (context: $OLLAMA_NUM_CTX)"
sedi "s|model: openai/[^ ]*|model: openai/$OLLAMA_MODEL|g" "$PROJECT_DIR/litellm/config.yaml"

echo ""
echo "Starting services..."
docker compose $COMPOSE_FILES up -d

echo ""
echo "Waiting for services to be ready..."
sleep 5

# Auto-pull model if not present
if $IS_MACOS; then
    if ollama list 2>/dev/null | grep -q "$OLLAMA_MODEL"; then
        echo "[+] $OLLAMA_MODEL model found"
    else
        echo "[!] $OLLAMA_MODEL not found. Pulling model..."
        "$SCRIPT_DIR/pull-model.sh" "$OLLAMA_MODEL"
    fi
else
    if docker exec krull-ollama ollama list 2>/dev/null | grep -q "$OLLAMA_MODEL"; then
        echo "[+] $OLLAMA_MODEL model found"
    else
        echo "[!] $OLLAMA_MODEL not found. Pulling model..."
        "$SCRIPT_DIR/pull-model.sh" "$OLLAMA_MODEL"
    fi
fi

echo ""

# Check each service
if $IS_MACOS; then
    # Check native Ollama
    if curl -sf http://localhost:11434/api/tags &>/dev/null; then
        echo "[+] ollama (native): running"
    else
        echo "[-] ollama (native): not running — start with 'ollama serve'"
    fi
    SERVICES="krull-webui krull-searxng krull-litellm krull-tileserver krull-photon krull-kiwix"
else
    SERVICES="krull-ollama krull-webui krull-searxng krull-litellm krull-tileserver krull-photon krull-kiwix"
fi
for svc in $SERVICES; do
    STATUS=$(docker inspect --format='{{.State.Status}}' "$svc" 2>/dev/null || echo "not found")
    if [ "$STATUS" = "running" ]; then
        echo "[+] $svc: running"
    else
        echo "[-] $svc: $STATUS"
    fi
done

# --- First boot: auto-run setup to provision filters + install krull-claude ---
# The sentinel data/.setup-complete is written by setup.sh on success.
# To force a re-run, delete it: rm data/.setup-complete && ./krull start
if [ ! -f "$PROJECT_DIR/data/.setup-complete" ]; then
    echo ""
    echo "============================================"
    echo "  First boot detected — running setup..."
    echo "============================================"
    echo ""
    "$SCRIPT_DIR/setup.sh"
fi

echo ""
echo "============================================"
echo "  Krull AI is running!"
echo ""
echo "  ➜  Krull Home:  http://localhost:8000  (start here)"
echo ""
echo "  Open WebUI:     http://localhost:3000"
echo "  LiteLLM:        http://localhost:4000"
echo "  Maps:           http://localhost:8070"
echo "  Kiwix:          http://localhost:8090"
echo "============================================"
