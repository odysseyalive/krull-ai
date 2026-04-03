# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Krull AI is a self-hosted AI stack combining chat UI, LLM inference, web search, and offline knowledge. It runs entirely via Docker Compose with five services:

- **Open WebUI** (port 3000) — Chat interface with RAG and web search integration
- **Ollama** — GPU-accelerated LLM inference (NVIDIA GPU required for acceleration)
- **LiteLLM** (port 4000) — API gateway that routes Anthropic/OpenAI API requests through Open WebUI, giving API callers access to the same pipelines, filters, and tools as the chat UI
- **SearXNG** — Metasearch engine providing web search to Open WebUI (no API keys needed)
- **Kiwix** (port 8090) — Offline Wikipedia/knowledge served from ZIM files

## Commands

```bash
./scripts/start.sh              # Check deps, pull images, start all services
./scripts/setup.sh              # Provision inlet filters into Open WebUI (run once)
./scripts/stop.sh               # Stop all services (data preserved)
./scripts/update.sh             # Pull latest images + force-recreate containers
./scripts/download-wikipedia.sh [mini|nopic|medicine|full]  # Download ZIM files for Kiwix
docker restart krull-kiwix      # Required after downloading new ZIM files
```

## Architecture

All services are defined in `docker-compose.yml`. Container names are prefixed `krull-`. Services communicate over Docker's default network using container names as hostnames (e.g., `http://krull-ollama:11434`, `http://krull-searxng:8080`).

Open WebUI connects to Ollama for inference and to SearXNG for RAG web search (configured via environment variables in the compose file). LiteLLM routes through Open WebUI's OpenAI-compatible API (`http://krull-webui:8080`), so API callers go through the same pipeline as the chat UI — its model mappings live in `litellm/config.yaml`. SearXNG config lives in `searxng/settings.yml` (bind-mounted). Kiwix serves ZIM files from the `zim/` directory (read-only mount).

Request flow: `Claude Code → LiteLLM (port 4000) → Open WebUI (pipelines/filters/tools) → Ollama → GPU`

Three global inlet filters are provisioned by `scripts/setup.sh` into Open WebUI: Context Manager (auto-compacts conversation history approaching context limits), Auto Web Search (queries SearXNG before each response), and Kiwix Knowledge Lookup (searches offline knowledge base). Source code for these lives in `functions/`. They apply to both browser and API traffic.

Data persistence: All data is stored in local bind mounts within the project directory. `data/ollama/` holds downloaded models, `data/webui/` holds WebUI config and chat history, `zim/` holds Kiwix ZIM files, and `searxng/` holds SearXNG config. This means data survives container recreation, image updates, and compose changes.

## Key Configuration

- `WEBUI_AUTH=False` — auth is disabled by default
- `WEBUI_SECRET_KEY` — defaults to a placeholder; set via env var for production
- SearXNG secret key in `searxng/settings.yml` is also a placeholder
- GPU passthrough uses `deploy.resources.reservations.devices` (requires `nvidia-container-toolkit` on host)

## Project Memory
This project uses an awareness ledger for institutional memory.

**Before recommending changes:** During research and planning, check
`.claude/skills/awareness-ledger/ledger/index.md` for relevant records. If
matching records exist, read them and factor their warnings, decisions, and
patterns into your recommendation. Use `/awareness-ledger consult` for full
agent-assisted analysis when high-risk overlap is detected.

**After resolving issues:** When you encounter bug investigations with root
causes, architectural decisions with trade-offs, or recurring patterns, ask
the user if they want to record the knowledge in the awareness ledger. Use
`/awareness-ledger record [type]` to capture it. Always finish the immediate
work first — suggest capture after, not during.
