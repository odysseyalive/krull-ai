#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# Default parameters tuned for voice profile + thinking mode
# Temperature 0.7 (down from default 1.0) prevents over-application of persona cues
# top_p 0.8 (down from 0.95) keeps responses grounded
DEFAULT_TEMPERATURE="0.7"
DEFAULT_TOP_P="0.8"
DEFAULT_TOP_K="20"
DEFAULT_PRESENCE_PENALTY="1.5"

usage() {
    echo "Usage: ./scripts/pull-model.sh <model> [model ...]"
    echo ""
    echo "Pulls models from Ollama and applies tuned parameters."
    echo ""
    echo "Examples:"
    echo "  ./scripts/pull-model.sh frob/qwen3.5-instruct:9b"
    echo "  ./scripts/pull-model.sh frob/qwen3.5-instruct:9b frob/qwen3.5-instruct:27b"
    echo "  ./scripts/pull-model.sh gemma4:26b"
    echo ""
    echo "Parameters applied to all models:"
    echo "  temperature:      $DEFAULT_TEMPERATURE (default is 1.0)"
    echo "  top_p:            $DEFAULT_TOP_P (default is 0.95)"
    echo "  top_k:            $DEFAULT_TOP_K"
    echo "  presence_penalty: $DEFAULT_PRESENCE_PENALTY"
    echo ""
    echo "Override with environment variables:"
    echo "  TEMPERATURE=0.6 ./scripts/pull-model.sh frob/qwen3.5-instruct:9b"
}

if [ $# -eq 0 ]; then
    usage
    exit 1
fi

TEMPERATURE="${TEMPERATURE:-$DEFAULT_TEMPERATURE}"
TOP_P="${TOP_P:-$DEFAULT_TOP_P}"
TOP_K="${TOP_K:-$DEFAULT_TOP_K}"
PRESENCE_PENALTY="${PRESENCE_PENALTY:-$DEFAULT_PRESENCE_PENALTY}"

# Detect platform
IS_MACOS=false
[[ "$OSTYPE" == "darwin"* ]] && IS_MACOS=true

# Check Ollama is running
if $IS_MACOS; then
    if ! curl -sf http://localhost:11434/api/tags &>/dev/null; then
        echo "ERROR: Ollama is not running."
        echo "       Start with: ollama serve"
        exit 1
    fi
else
    if ! docker inspect --format='{{.State.Status}}' krull-ollama 2>/dev/null | grep -q "running"; then
        echo "ERROR: krull-ollama is not running."
        echo "       Start services first: ./scripts/start.sh"
        exit 1
    fi
fi

for MODEL in "$@"; do
    echo "Pulling $MODEL..."
    if $IS_MACOS; then
        ollama pull "$MODEL"
    else
        docker exec krull-ollama ollama pull "$MODEL"
    fi

    echo "Applying tuned parameters (temp=$TEMPERATURE, top_p=$TOP_P)..."
    MODELFILE="FROM $MODEL
PARAMETER temperature $TEMPERATURE
PARAMETER top_p $TOP_P
PARAMETER top_k $TOP_K
PARAMETER presence_penalty $PRESENCE_PENALTY"

    if $IS_MACOS; then
        TMPFILE=$(mktemp)
        echo "$MODELFILE" > "$TMPFILE"
        ollama create "$MODEL" -f "$TMPFILE"
        rm -f "$TMPFILE"
    else
        docker exec krull-ollama bash -c "cat > /tmp/Modelfile << EOF
$MODELFILE
EOF
ollama create $MODEL -f /tmp/Modelfile"
    fi

    echo "[+] $MODEL ready (tuned)"
    echo ""
done

echo "Done. Models available:"
if $IS_MACOS; then
    ollama list
else
    docker exec krull-ollama ollama list
fi
