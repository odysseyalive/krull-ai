# Krull AI — Technical Reference

This document holds the deep technical material that used to live in `README.md`. The README is now an install-and-getting-started guide; everything CLI, architectural, or operational lives here.

---

## Table of contents

- [Service inventory](#service-inventory)
- [The `./krull` CLI](#the-krull-cli)
- [Pulling models](#pulling-models)
- [Building the offline library](#building-the-offline-library)
  - [Wikipedia](#wikipedia)
  - [Knowledge packages and bundles](#knowledge-packages-and-bundles)
  - [Project Gutenberg](#project-gutenberg)
- [Offline maps](#offline-maps)
  - [Map types and layers](#map-types-and-layers)
  - [Base map regions](#base-map-regions)
  - [Nautical chart regions](#nautical-chart-regions)
  - [Aeronautical chart regions](#aeronautical-chart-regions)
  - [Terrain and hillshade](#terrain-and-hillshade)
  - [Managing downloads](#managing-downloads)
  - [Map features](#map-features)
- [Using with Claude Code](#using-with-claude-code)
  - [The `krull-claude` command](#the-krull-claude-command)
  - [Model mapping](#model-mapping)
  - [Environment configuration](#environment-configuration)
  - [Feature compatibility matrix](#feature-compatibility-matrix)
- [Architecture](#architecture)
- [Filters](#filters)
- [Data and persistence](#data-and-persistence)
- [GPU](#gpu)
- [Troubleshooting](#troubleshooting)

---

## Service inventory

| Service | Container | Port | Purpose |
|---|---|---|---|
| **Krull Home** | `krull-home` | 8000 | Project homepage, library installer, .env editor |
| **Open WebUI** | `krull-webui` | 3000 | Browser chat with RAG, tools, and pipelines |
| **Ollama** | `krull-ollama` | internal | GPU-accelerated LLM inference |
| **LiteLLM** | `krull-litellm` | 4000 | API gateway for Claude Code |
| **SSE Proxy** | `krull-sse-proxy` | 4001 | Anthropic-format streaming + filter pipeline for Claude Code traffic |
| **SearXNG** | `krull-searxng` | internal | Web search aggregator (Google, DuckDuckGo, Brave) |
| **Martin tile server** | `krull-tileserver` | internal | Serves PMTiles + MBTiles to the map viewer |
| **Map viewer** | `krull-map-viewer` | 8070 | Offline map UI built on MapLibre GL |
| **Photon** | `krull-photon` | 2322 | Offline geocoding + place search |
| **Kiwix** | `krull-kiwix` | 8090 | Serves ZIM files (Wikipedia, dev docs, knowledge archives) |

All services restart on failure (`unless-stopped`). Containers communicate over the default Docker network using container names as hostnames.

---

## The `./krull` CLI

Every administrative action is wrapped by `./krull` at the project root.

| Command | What it does |
|---|---|
| `./krull start` | Checks dependencies (with OS-specific install hints), pulls images, starts everything |
| `./krull stop` | Stops all services. Your data stays. |
| `./krull setup` | Installs filters into Open WebUI, installs `krull-claude`. Run once after first install. |
| `./krull update` | Pulls latest images and recreates containers. Your data stays. |
| `./krull pull-model <model>` | Pulls Ollama models with tuned parameters (temperature 0.7, top_p 0.8) |
| `./krull download-wikipedia [mini\|nopic\|medicine\|full]` | Downloads Wikipedia ZIM files for Kiwix |
| `./krull download-knowledge <package\|bundle> [...]` | Downloads dev docs, Stack Exchange, knowledge archives, and more |
| `./krull download-maps <region>` | Downloads all map types for a region (base + terrain + nautical + aero) |

Individual scripts in `scripts/` still work directly if needed. The Library of Alexandria page on the homepage (`http://localhost:8000/library`) is a UI wrapper around `download-knowledge`, `download-wikipedia`, and `download-maps`.

---

## Pulling models

You need at least one LLM. Pick based on your GPU memory:

| Model | VRAM | Good for |
|---|---|---|
| `frob/qwen3.5-instruct:4b` | ~3 GB | Quick responses, lighter hardware |
| `frob/qwen3.5-instruct:9b` | ~6 GB | **Recommended** — coding + tool calling |
| `frob/qwen3.5-instruct:27b` | ~16 GB | Best quality (needs beefy GPU) |

```bash
./krull pull-model frob/qwen3.5-instruct:9b
```

This pulls the model and applies tuned parameters (temperature 0.7, top_p 0.8) that work well with system prompts and persona-driven use. The defaults ship at temperature 1.0, which is too hot.

> **Why this model?** The `frob/qwen3.5-instruct` variant is qwen3.5 with thinking mode disabled — same weights, same quality, faster responses without `<think>` blocks. It produces proper Anthropic-style `tool_use` blocks, which Claude Code requires. The qwen2.5-coder models output tool calls as JSON text instead, which breaks tool calling entirely.

You can pull multiple models at once, or override parameters:

```bash
./krull pull-model frob/qwen3.5-instruct:9b frob/qwen3.5-instruct:27b
TEMPERATURE=0.6 ./krull pull-model frob/qwen3.5-instruct:9b
```

---

## Building the offline library

The Library of Alexandria page on the homepage is the easiest way to browse and install everything below. The CLI commands documented here are the same ones the homepage runs under the hood — useful for scripting, automation, or when you want to fire off a 200 GB download from a tmux session and walk away.

### Wikipedia

```bash
./krull download-wikipedia mini       # Top 100 articles (~5 MB) — quick test
./krull download-wikipedia medicine   # Medical articles (~2 GB)
./krull download-wikipedia nopic      # All articles, no images (~25 GB)
./krull download-wikipedia full       # Everything with images (~115 GB)
docker restart krull-kiwix
```

### Knowledge packages and bundles

```bash
./krull download-knowledge dev-essentials      # Python, JS, TS, Node, Git, Docker, Bash (~50 MB)
./krull download-knowledge web-dev              # React, Next.js, Tailwind, PHP, MariaDB, etc. (~58 MB)
./krull download-knowledge krull-stack          # Everything this repo itself uses (~20 MB)
./krull download-knowledge data-science         # Python, NumPy, Pandas, Scikit-learn (~75 MB)
./krull download-knowledge sysadmin             # Arch Wiki, Unix & Server Q&A (~5.5 GB)
./krull download-knowledge community            # Code Review, Security, SoftEng Q&A (~5 GB)
./krull download-knowledge cooking-essentials   # Food prep, recipes, Seasoned Advice Q&A (~360 MB)
./krull download-knowledge survival-essentials  # Post-disaster, field medicine, water, outdoors, appropedia, wikivoyage (~1.9 GB)
docker restart krull-kiwix
```

The `survival-essentials` bundle pulls field-medicine, military-medicine (FAS), post-disaster prep, water purification, outdoor wilderness Q&A, Appropedia, Wikivoyage, and curated food preparation. Add-ons available individually: `gardening-stackexchange` (~925 MB), `energypedia` (~799 MB), `ifixit` (~3.5 GB).

Run `./krull download-knowledge` with no arguments for the full catalog (60+ individual packages).

### Project Gutenberg

```bash
./krull download-knowledge gutenberg-fiction       # Novels & stories (~20 GB)
./krull download-knowledge gutenberg-science       # Science texts (~12 GB)
./krull download-knowledge gutenberg-essentials    # Classic lit bundle (~65 GB)
./krull download-knowledge gutenberg-stem          # Science/tech/medicine (~22 GB)
./krull download-knowledge gutenberg               # Everything (~206 GB)
docker restart krull-kiwix
```

18 categories available by Library of Congress classification.

---

## Offline maps

The map viewer at <http://localhost:8070> serves three types of offline maps with terrain visualization, search, and measurement tools.

### Map types and layers

| Style | Source | What it shows |
|---|---|---|
| **Topo** (Light/Dark/etc.) | OpenStreetMap + terrain DEM | Roads, buildings, terrain hillshade, contour lines, 3D terrain |
| **Nautical** | NOAA Chart Display Service | Depth soundings, buoys, shipping lanes, navigational aids, bottom composition |
| **Aeronautical** | FAA VFR Sectional Charts | Airports, airspace classes, navigation aids, obstacles, terrain shading |

When you run `./krull download-maps <region>`, it downloads four data types:

1. **Base tiles** — OpenStreetMap vector tiles (roads, buildings, water, land use)
2. **Terrain** — Global elevation data for hillshade, contour lines, and 3D terrain
3. **Nautical charts** — NOAA NCDS MBTiles, auto-detected by coastal overlap
4. **Aeronautical charts** — FAA VFR Sectional GeoTIFFs, converted to MBTiles via GDAL

Use `--base-only` if you only want OSM tiles without terrain/nautical/aero.

### Base map regions

| Region | Size | Description |
|---|---|---|
| `oregon` | ~100 MB | Oregon |
| `washington` | ~100 MB | Washington |
| `california` | ~300 MB | California |
| `colorado` | ~80 MB | Colorado |
| `new-york` | ~150 MB | New York |
| `texas` | ~300 MB | Texas |
| `us-west` | ~800 MB | US West (WA to CA to CO) |
| `us` | ~3 GB | United States |
| `europe` | ~5 GB | Europe |
| `planet` | ~80 GB | Entire planet |

### Nautical chart regions

NOAA charts are auto-detected when downloading a region, or can be downloaded separately:

```bash
./krull download-maps --nautical pacific-nw       # OR/WA coast (~800 MB)
./krull download-maps --nautical california        # CA coast (~1.1 GB)
./krull download-maps --nautical us-east-coast     # ME to FL (~4.5 GB)
./krull download-maps --nautical gulf-of-mexico    # FL to TX (~2.0 GB)
```

Source: NOAA Chart Display Service (NCDS). Updated weekly. Charts include bathymetry, depth soundings, buoys, shipping lanes, navigational aids, and bottom composition.

### Aeronautical chart regions

```bash
./krull download-maps --aeronautical pacific-nw    # Seattle + Klamath Falls (~175 MB)
./krull download-maps --aeronautical california     # SF + LA + Klamath Falls (~235 MB)
./krull download-maps --aeronautical southwest      # Las Vegas + Phoenix + SLC (~275 MB)
```

Source: FAA VFR Sectional Charts. Updated every 56 days. Charts include airports, airspace classes, navigation aids, obstacles, terrain shading, and flight training areas.

### Terrain and hillshade

Terrain data downloads automatically with a region. To download separately:

```bash
./krull download-maps --terrain global    # Whole planet, zoom 0-9, ~700 MB
./krull download-maps --terrain oregon    # High detail (z0-12) for a specific region
```

Toggle hillshade, contour lines, and 3D terrain in the map viewer's sidebar under **Overlays**.

### Managing downloads

```bash
./krull download-maps list            # See all regions and what's downloaded
./krull download-maps status          # Show downloaded files and sizes
./krull download-maps remove oregon   # Delete a specific tile file
```

You can do all of this from the homepage's Library page too — but the CLI is faster for batch operations.

### Map features

- **3 map styles**: Topo (5 color themes), Nautical (NOAA charts), Aeronautical (FAA VFR)
- **Search**: Hybrid offline search — place index + viewport tile features + Photon geocoding
- **Measure tool**: Click points to measure distances, multi-segment paths, imperial/metric toggle
- **Terrain overlays**: Hillshade, contour lines (ft/m), 3D terrain
- **Label toggles**: Show/hide place names, road names, water names, POI labels independently
- **Units**: Imperial (mi/ft) or Metric (km/m)
- **Coordinates**: Live lat/lon display, click-to-copy
- **URL hash**: Bookmarkable map positions (`#zoom/lat/lon`)

All downloaded data is stored in `data/tiles/` and survives container rebuilds.

---

## Using with Claude Code

### The `krull-claude` command

```bash
krull-claude
```

The `krull-claude` command is installed to `~/.local/bin/` when you run `./krull setup`. It launches Claude Code pre-configured to talk to your local stack. Requires `~/.local/bin` to be in your `$PATH`. All arguments are passed through to `claude`, so `krull-claude -p "hello"` works as expected.

<details>
<summary>Manual alternative (without krull-claude)</summary>

```bash
ANTHROPIC_AUTH_TOKEN=sk-local-dev-key \
ANTHROPIC_BASE_URL=http://localhost:4000 \
DISABLE_TELEMETRY=1 \
CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
claude
```

</details>

> **Note:** `ANTHROPIC_AUTH_TOKEN` (not `ANTHROPIC_API_KEY`) skips Claude Code's login screen. The telemetry flags prevent Claude Code from hitting Ollama's unsupported `/v1/messages/count_tokens` endpoint, which causes cascading failures.

### Model mapping

Claude Code sends requests using Anthropic model names. LiteLLM maps them to your local models via the SSE proxy (which applies the search/knowledge filters). Edit `litellm/config.yaml`:

```yaml
general_settings:
  allow_requests_on_db_unavailable: true

litellm_settings:
  drop_params: true

model_list:
  - model_name: claude-sonnet-4-6
    litellm_params:
      model: openai/frob/qwen3.5-instruct:9b
      api_base: http://krull-sse-proxy:8081
      api_key: "sk-e4c0de164d854d4dbd003556033363c2"
```

All Claude model names (`claude-sonnet-4-6`, `claude-opus-4-6`, `claude-haiku-4-5-20251001`, etc.) should map to the same local model through the SSE proxy. The proxy applies inlet filters (date, Kiwix knowledge, web search, map search, context compaction) before forwarding to Ollama.

> **Note:** The `openai/` provider routes through the SSE proxy which handles Anthropic-to-Ollama translation and tool call formatting. `drop_params: true` silently drops Anthropic-specific parameters (like `cache_control`) that Ollama doesn't support. `allow_requests_on_db_unavailable` prevents "No connected db" errors since we run without a database.

### Environment configuration

Copy `.env.sample` to `.env` to customize settings, or just use the **Settings** page on the homepage (`http://localhost:8000/settings`) which gives you a typed form for every known variable.

| Variable | Default | What it does |
|---|---|---|
| `OLLAMA_NUM_CTX` | `131072` | Context window size (tokens) |
| `OLLAMA_TEMPERATURE` | `0.8` | Sampling temperature |
| `OLLAMA_TOP_P` | `0.8` | Nucleus sampling threshold |
| `OLLAMA_TOP_K` | `20` | Top-K sampling |
| `OLLAMA_PRESENCE_PENALTY` | `1.5` | Discourages topic repetition |
| `CONTEXT_COMPACT_LIMIT` | `98304` | Auto-compact threshold (~75% of context) |
| `WEBUI_SECRET_KEY` | `changeme...` | Open WebUI session key |
| `LITELLM_MASTER_KEY` | `sk-local-dev-key` | API key for Claude Code |
| `PHOTON_COUNTRY_CODE` | (empty) | Restrict geocoding to a country (e.g., `US`) |
| `FAA_EDITION` | `03-19-2026` | FAA chart edition date (updates every 56 days) |

### Feature compatibility matrix

| Feature | Status | Notes |
|---|---|---|
| Code generation | Works well | Quality scales with model size |
| File read/write/edit | Works | Standard tool calling |
| Bash commands | Works | Standard tool calling |
| Git operations | Works | Standard tool calling |
| Skills (`/commit`, `/simplify`, etc.) | Works | Claude Code handles skill expansion natively |
| Plan mode (`/plan`) | Works | Claude Code's built-in plan mode protocol |
| Plan execution | Works | Claude Code tracks plan steps natively |
| Web search context | Works | Auto Web Search injects results before every response |
| Offline knowledge | Works | Kiwix Lookup injects relevant articles |
| Map/location queries | Works | Offline Map Search finds places and coordinates |
| Context management | Works | Context Manager auto-compacts long conversations |
| Hooks | Works | Hooks run in the Claude Code CLI, independent of the model |
| Extended thinking | Limited | Depends on model capability |
| Multi-agent coordination | Limited | Smaller models struggle with parallel agents |

---

## Architecture

```
                           +------------------------------------------+
                           |              Open WebUI                  |
  Browser ----------------►|  truth guard, date/time, web search,     |
                           |  kiwix, map search, context compaction,  |
                           |  pipelines, RAG                          |
                           +-------------------+----------------------+
                                               |
                                               v
                                            Ollama --> GPU
                                               ^
                                               |
                           +-------------------+----------------------+
                           |           SSE Proxy (port 4001)          |
  Claude Code --> LiteLLM -►|  truth guard, date/time, web search,     |
                 (port 4000)|  kiwix, map search, context compaction,  |
                           |  tool filtering, SSE streaming            |
                            +-----------------------------------------+
```

Two paths to the same brain. The browser talks to Open WebUI directly. Claude Code goes through LiteLLM, which translates the Anthropic API format and routes to the SSE proxy. Both paths run the same set of filters — truth guard, date/time injection, SearXNG web search, Kiwix offline knowledge, Photon map search, and context compaction. The SSE proxy additionally handles tool call filtering and streams responses back as Anthropic-compatible SSE events. Skills, plan mode, and hooks are handled natively by Claude Code on the client side.

Krull Home (`http://localhost:8000`) is a separate front door that doesn't sit in the request path of the AI traffic — it's a pure portal + admin UI. It talks to the Docker socket to report container health and to restart `krull-kiwix` and `krull-tileserver` after library installs/deletes.

---

## Filters

Local models forget. They lose track of plans. They ignore constraints. They don't know what day it is. These filters compensate.

They're Python functions that process every request before it reaches the model. The `setup.sh` script installs them globally in Open WebUI:

| Filter | What it does |
|---|---|
| **Current Date & Time** | Injects today's date and current time into every request. Local models have no sense of time otherwise. |
| **Context Manager** | Monitors token usage and auto-compacts older messages when the conversation approaches the context limit. Keeps things alive instead of crashing. |
| **Auto Web Search** | Queries SearXNG before every response and injects the top results with source URLs. The model cites what it finds. |
| **Kiwix Knowledge Lookup** | Searches the offline knowledge base and injects relevant article snippets. Works without internet. |
| **Offline Map Search** | Detects location queries and searches Photon for places, addresses, and coordinates. Results include links to the local tile server. |

All filter settings are adjustable in Open WebUI under **Admin Panel > Functions > [filter name] > Valves**.

Settings you might want to tune:

| Setting | Filter | Default | What to change |
|---|---|---|---|
| `max_context_tokens` | Context Manager | 16384 | Match your model's actual context window |
| `compact_threshold` | Context Manager | 0.75 | Lower = more aggressive compaction |
| `num_results` | Auto Web Search | 5 | Fewer results = less context used |
| `enabled` | Any filter | true | Disable what you don't need |

---

## Data and persistence

Everything lives in local directories. Not Docker volumes. Not ephemeral storage. You can tear down containers, update images, change the compose file. Your models, chat history, maps, and knowledge stay right where they are.

| Directory | What's there |
|---|---|
| `data/ollama/` | Downloaded LLM models |
| `data/webui/` | Chat history, documents, settings, RAG embeddings |
| `data/tiles/` | Map tiles — OSM (PMTiles), NOAA nautical (MBTiles), FAA aero (MBTiles), terrain (PMTiles) |
| `data/photon/` | Photon geocoding index |
| `functions/` | Inlet filter source code (Python) |
| `sample_prompts/` | Example system prompts for Open WebUI |
| `litellm/` | LiteLLM model mapping config |
| `searxng/` | SearXNG search engine config |
| `zim/` | Kiwix ZIM files (Wikipedia, dev docs, etc.) |
| `krull-home/` | Source for the Krull Home service (frontend + Express backend) |

---

## GPU

Ollama uses your NVIDIA GPU for inference. You need `nvidia-container-toolkit` on the host. The start script checks for this and tells you how to install it.

Without a GPU, Ollama falls back to CPU. It works. It's just slow.

---

## Troubleshooting

**Services won't start:** Run `./krull start`. It checks everything and tells you what's missing.

**Model not responding:** Pull at least one model first: `docker exec krull-ollama ollama pull frob/qwen3.5-instruct:9b`

**Claude Code can't connect:** Check that LiteLLM is running (`docker logs krull-litellm`) and that your model name in `litellm/config.yaml` matches what Claude Code expects. Use `ANTHROPIC_AUTH_TOKEN` (not `ANTHROPIC_API_KEY`) to skip the login screen.

**"No connected db" error:** Make sure `litellm/config.yaml` has `general_settings.allow_requests_on_db_unavailable: true`. This is required because LiteLLM runs without a database in this stack.

**Filters not working:** Run `./krull setup` again. Check **Admin Panel > Functions** in Open WebUI to verify they're listed and enabled.

**Out of GPU memory:** Try a smaller model (`frob/qwen3.5-instruct:4b`) or a quantized variant like `frob/qwen3.5-instruct:9b-q4_K_M`.

**FAA chart edition expired:** The FAA updates VFR charts every 56 days. If downloads fail, update the edition date: `FAA_EDITION=MM-DD-YYYY ./krull download-maps --aeronautical pacific-nw`

**Krull Home can't restart containers:** The `krull-home` container needs the Docker socket bind-mounted. Check `docker-compose.yml` includes `- /var/run/docker.sock:/var/run/docker.sock` in the `krull-home` volumes section.

**Library install hangs at 0%:** The bash script runs but the file isn't appearing yet. Check `docker logs krull-home` for the spawned script's error output. If you're behind a corporate proxy, the underlying `curl` may be timing out — try the equivalent CLI command (e.g., `./krull download-knowledge devdocs-python`) directly to see the network error.

**Starting fresh:** `docker compose down` stops everything. Data in `data/` is preserved. To truly wipe: delete the `data/` directory and run `./krull start` again.
