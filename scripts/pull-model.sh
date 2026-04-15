#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# Source .env for model parameters if available
ENV_FILE="$PROJECT_DIR/.env"
if [ -f "$ENV_FILE" ]; then
    set -a; source "$ENV_FILE"; set +a
fi

# Defaults tuned for voice profile + persona work
# Temperature 0.8 gives enough variety for natural cadence without losing coherence
# top_p 0.8 (down from 0.95) keeps responses grounded
DEFAULT_TEMPERATURE="${OLLAMA_TEMPERATURE:-0.8}"
DEFAULT_TOP_P="${OLLAMA_TOP_P:-0.8}"
DEFAULT_TOP_K="${OLLAMA_TOP_K:-20}"
DEFAULT_PRESENCE_PENALTY="${OLLAMA_PRESENCE_PENALTY:-1.5}"
# num_ctx 8192 (up from 4096 default) gives Qwen 3.5 enough room to retain
# its own thinking trace across multi-turn conversations without forcing a
# context-eviction mid-trace. Fits comfortably in 16 GB VRAM.
# num_predict 4096 caps any single response so a runaway generation past
# EOS (Qwen sometimes emits <|im_start|>user and hallucinates a fake user
# turn) cannot expand to tens of thousands of tokens.
DEFAULT_NUM_CTX="${OLLAMA_NUM_CTX:-8192}"
DEFAULT_NUM_PREDICT="${OLLAMA_NUM_PREDICT:-4096}"

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
    echo "  num_ctx:          $DEFAULT_NUM_CTX"
    echo "  num_predict:      $DEFAULT_NUM_PREDICT"
    echo ""
    echo "Set in .env (persistent) or override per-run:"
    echo "  .env:    OLLAMA_TEMPERATURE=0.6"
    echo "  Per-run: TEMPERATURE=0.6 ./scripts/pull-model.sh frob/qwen3.5-instruct:9b"
}

if [ $# -eq 0 ]; then
    usage
    exit 1
fi

TEMPERATURE="${TEMPERATURE:-$DEFAULT_TEMPERATURE}"
TOP_P="${TOP_P:-$DEFAULT_TOP_P}"
TOP_K="${TOP_K:-$DEFAULT_TOP_K}"
PRESENCE_PENALTY="${PRESENCE_PENALTY:-$DEFAULT_PRESENCE_PENALTY}"
NUM_CTX="${NUM_CTX:-$DEFAULT_NUM_CTX}"
NUM_PREDICT="${NUM_PREDICT:-$DEFAULT_NUM_PREDICT}"

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

    echo "Applying tuned parameters (temp=$TEMPERATURE, top_p=$TOP_P, num_ctx=$NUM_CTX, num_predict=$NUM_PREDICT)..."
    MODELFILE="FROM $MODEL
PARAMETER temperature $TEMPERATURE
PARAMETER top_p $TOP_P
PARAMETER top_k $TOP_K
PARAMETER presence_penalty $PRESENCE_PENALTY
PARAMETER num_ctx $NUM_CTX
PARAMETER num_predict $NUM_PREDICT"

    # Per-family stop tokens. Without these, Ollama's built-in chat
    # template renderer/parser does not always halt at end-of-turn for
    # Qwen 3.5 (the model emits <|endoftext|><|im_start|>user and
    # then hallucinates a fake user turn). Gemma 4 has the same risk
    # with <end_of_turn>. We add them explicitly so the running tag
    # halts cleanly regardless of renderer behavior.
    LOWER_MODEL=$(echo "$MODEL" | tr '[:upper:]' '[:lower:]')
    case "$LOWER_MODEL" in
        *qwen*)
            MODELFILE="$MODELFILE
PARAMETER stop \"<|im_end|>\"
PARAMETER stop \"<|im_start|>\"
PARAMETER stop \"<|endoftext|>\""
            ;;
        *gemma*)
            MODELFILE="$MODELFILE
PARAMETER stop \"<end_of_turn>\"
PARAMETER stop \"<start_of_turn>\"
PARAMETER stop \"<eos>\""
            ;;
    esac

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
