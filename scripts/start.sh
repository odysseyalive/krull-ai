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
                curl)           echo "  brew install curl" ;;
                nvidia-container-toolkit)
                    echo "  NVIDIA GPU passthrough is not supported on macOS." ;;
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

# Check NVIDIA
if docker info 2>/dev/null | grep -qi nvidia; then
    echo "[+] NVIDIA GPU runtime detected"
else
    echo "[!] WARNING: NVIDIA runtime not detected. Ollama will use CPU only."
    install_hint nvidia-container-toolkit
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
echo "Pulling latest images..."
docker compose -f "$PROJECT_DIR/docker-compose.yml" pull

echo ""
echo "Starting services..."
docker compose -f "$PROJECT_DIR/docker-compose.yml" up -d

echo ""
echo "Waiting for services to be ready..."
sleep 5

# Auto-pull frob/qwen3.5-instruct:9b if not present (recommended model for tool calling)
if docker exec krull-ollama ollama list 2>/dev/null | grep -q "frob/qwen3.5-instruct:9b"; then
    echo "[+] frob/qwen3.5-instruct:9b model found"
else
    echo "[!] frob/qwen3.5-instruct:9b not found. Pulling recommended model (~6.6 GB)..."
    "$SCRIPT_DIR/pull-model.sh" frob/qwen3.5-instruct:9b
fi

echo ""

# Check each service
for svc in krull-ollama krull-webui krull-searxng krull-litellm krull-tileserver krull-photon krull-kiwix; do
    STATUS=$(docker inspect --format='{{.State.Status}}' "$svc" 2>/dev/null || echo "not found")
    if [ "$STATUS" = "running" ]; then
        echo "[+] $svc: running"
    else
        echo "[-] $svc: $STATUS"
    fi
done

echo ""
echo "============================================"
echo "  Krull AI is running!"
echo ""
echo "  Open WebUI:  http://localhost:3000"
echo "  LiteLLM:     http://localhost:4000"
echo "  Maps:        http://localhost:8070"
echo "  Kiwix:       http://localhost:8090"
echo "============================================"
