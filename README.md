# KRULL AI

**Knowledge, Reasoning, and Unified Local Learning**

A self-hosted AI stack that runs entirely on your own hardware. Local inference, web search, offline knowledge, geocoding, maps, and an API gateway that lets [Claude Code](https://claude.ai/code) talk to your local models. Designed to drop into your existing Claude Code projects — your skills, hooks, CLAUDE.md files, and workflows keep working as-is. You shouldn't have to change how you use Claude Code just because the brain behind it is local. No cloud dependencies. No API keys. No internet required after setup.

## What's Inside

| Service | What it does | URL |
|---|---|---|
| **Open WebUI** | Browser-based chat with RAG, tools, and pipelines | <http://localhost:3000> |
| **Ollama** | GPU-accelerated LLM inference | internal |
| **LiteLLM** | API gateway so Claude Code can talk to local models | <http://localhost:4000> |
| **SearXNG** | Web search aggregator (Google, DuckDuckGo, Brave) | internal |
| **Martin** | Offline map tile server (PMTiles + MBTiles) | <http://localhost:8070> |
| **Photon** | Offline geocoding and place search | internal |
| **Kiwix** | Offline Wikipedia and knowledge archives | <http://localhost:8090> |

## Prerequisites

- **Docker** and **Docker Compose** (v2 plugin)
- **curl**
- **NVIDIA GPU** with `nvidia-container-toolkit` (optional, but recommended)

The start script checks for all of these and tells you exactly what to install for your OS. Arch, Ubuntu, Fedora, macOS, the works.

## Getting Started

### 1. Clone and start

```bash
git clone git@github.com:odysseyalive/krull-ai.git
cd krull-ai
./krull start
```

First run pulls Docker images (~10 GB), downloads a small Wikipedia starter (~5 MB), and starts everything. It takes a few minutes, then you're up.

### 2. Pull a model

You need at least one LLM. Pick based on your GPU memory:

| Model | VRAM | Good for |
|---|---|---|
| `frob/qwen3.5-instruct:4b` | ~3 GB | Quick responses, lighter hardware |
| `frob/qwen3.5-instruct:9b` | ~6 GB | **Recommended** — coding + tool calling |
| `frob/qwen3.5-instruct:27b` | ~16 GB | Best quality (needs beefy GPU) |

```bash
./krull pull-model frob/qwen3.5-instruct:9b
```

This pulls the model and applies tuned parameters (temperature 0.7, top_p 0.8) that work well with system prompts and thinking mode. The defaults ship at temperature 1.0, which is too hot for persona-driven use.

> **Why this model?** The `frob/qwen3.5-instruct` variant is qwen3.5 with thinking mode disabled — same weights, same quality, but faster responses without `<think>` blocks. It produces proper Anthropic-style `tool_use` blocks, which Claude Code requires. The qwen2.5-coder models output tool calls as JSON text instead, which breaks tool calling entirely.

You can pull multiple models at once, or override parameters:

```bash
./krull pull-model frob/qwen3.5-instruct:9b frob/qwen3.5-instruct:27b        # Pull multiple
TEMPERATURE=0.6 ./krull pull-model frob/qwen3.5-instruct:9b     # Override temp for coding
```

### 3. Provision the filters

```bash
./krull setup
```

This installs a set of intelligent filters into Open WebUI that compensate for what local models lack compared to cloud models. Context management, automatic web search, offline knowledge injection, plan mode guardrails, and more. It also installs the `krull-claude` command to `~/.local/bin/`. Run once after first install.

### 4. Start chatting

Open <http://localhost:3000>. Pick your model. Ask it something.

### 5. Build your offline library

The real power here shows up when the internet isn't available. Stock up while you can.

**Maps** (topo + terrain + nautical charts + aeronautical charts):

```bash
./krull download-maps oregon             # Downloads everything for Oregon
docker restart krull-tileserver
```

This downloads base OSM tiles, global terrain/hillshade, NOAA nautical charts, and FAA VFR sectional charts for the region — all auto-detected by geographic overlap.

**Wikipedia:**

```bash
./krull download-wikipedia medicine    # Medical articles (~2 GB)
./krull download-wikipedia nopic       # All articles, no images (~25 GB)
./krull download-wikipedia full        # Everything with images (~115 GB)
docker restart krull-kiwix
```

**Developer docs, Stack Exchange, and more:**

```bash
./krull download-knowledge dev-essentials    # Python, JS, TS, Node, Git, Docker, Bash (~50 MB)
./krull download-knowledge web-dev            # Full web stack including PHP, MariaDB (~55 MB)
./krull download-knowledge data-science       # Python, NumPy, Pandas, Scikit-learn (~75 MB)
./krull download-knowledge sysadmin           # Arch Wiki, Unix & Server Q&A (~5.5 GB)
./krull download-knowledge community          # Code Review, Security, SoftEng Q&A (~5 GB)
docker restart krull-kiwix
```

**Project Gutenberg (60,000+ free books):**

```bash
./krull download-knowledge gutenberg-fiction       # Novels & stories (~20 GB)
./krull download-knowledge gutenberg-science       # Science texts (~12 GB)
./krull download-knowledge gutenberg-essentials    # Classic lit bundle (~65 GB)
./krull download-knowledge gutenberg-stem          # Science/tech/medicine (~22 GB)
./krull download-knowledge gutenberg               # Everything (~206 GB)
docker restart krull-kiwix
```

18 categories available by Library of Congress classification. Run `./krull download-knowledge` with no arguments to see the full catalog.

See [Offline Maps](#offline-maps) below for the full map guide. Photon geocoding data downloads automatically on first start.

## Offline Maps

The map viewer at <http://localhost:8070> serves three types of offline maps with terrain visualization, search, and measurement tools. Everything runs locally — no internet needed after download.

### Quick start

```bash
./krull download-maps oregon          # All map types for Oregon
docker restart krull-tileserver
```

Open <http://localhost:8070>. You get three map styles you can switch between in the sidebar:

| Style | Source | What it shows |
|---|---|---|
| **Topo** (Light/Dark/etc.) | OpenStreetMap + terrain DEM | Roads, buildings, terrain hillshade, contour lines, 3D terrain |
| **Nautical** | NOAA Chart Display Service | Depth soundings, buoys, shipping lanes, navigational aids, bottom composition |
| **Aeronautical** | FAA VFR Sectional Charts | Airports, airspace classes, navigation aids, obstacles, terrain shading |

### What gets downloaded

When you run `./krull download-maps <region>`, it downloads all four data types:

1. **Base tiles** — OpenStreetMap vector tiles (roads, buildings, water, land use)
2. **Terrain** — Global elevation data for hillshade, contour lines, and 3D terrain
3. **Nautical charts** — NOAA NCDS MBTiles, auto-detected by coastal overlap
4. **Aeronautical charts** — FAA VFR Sectional GeoTIFFs, converted to MBTiles via GDAL

Use `--base-only` if you only want the OpenStreetMap tiles without terrain/nautical/aero.

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

FAA charts are auto-detected when downloading a region, or can be downloaded separately:

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

Toggle hillshade, contour lines, and 3D terrain in the sidebar under **Overlays**.

### Managing downloads

```bash
./krull download-maps list            # See all regions and what's downloaded
./krull download-maps status          # Show downloaded files and sizes
./krull download-maps remove oregon   # Delete a specific tile file
```

### Map features

- **3 map styles**: Topo (5 color themes), Nautical (NOAA charts), Aeronautical (FAA VFR)
- **Search**: Hybrid offline search — place index + viewport tile features + Photon geocoding
- **Measure tool**: Click points to measure distances, multi-segment paths, imperial/metric toggle
- **Terrain overlays**: Hillshade, contour lines (ft/m), 3D terrain
- **Label toggles**: Show/hide place names, road names, water names, POI labels independently
- **Units**: Imperial (mi/ft) or Metric (km/m) — affects scale bar and measure tool
- **Coordinates**: Live lat/lon display, click-to-copy
- **URL hash**: Bookmarkable map positions (`#zoom/lat/lon`)

All downloaded data is stored in `data/tiles/` and survives container rebuilds, image updates, and compose changes.

## Using with Claude Code

This got me thinking. What if you could keep coding with Claude Code even when the internet disappears? That's what this stack is for.

### Connect Claude Code to your local stack

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

### Configure model mapping

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

Copy `.env.sample` to `.env` to customize settings. The start script auto-generates `.env` with GPU detection, but you can override:

| Variable | Default | What it does |
|---|---|---|
| `OLLAMA_NUM_CTX` | `131072` | Context window size (tokens) |
| `CONTEXT_COMPACT_LIMIT` | `98304` | Auto-compact threshold (~75% of context) |
| `WEBUI_SECRET_KEY` | `changeme...` | Open WebUI session key |
| `LITELLM_MASTER_KEY` | `sk-local-dev-key` | API key for Claude Code |
| `PHOTON_COUNTRY_CODE` | (empty) | Restrict geocoding to a country (e.g., `US`) |
| `FAA_EDITION` | `03-19-2026` | FAA chart edition date (updates every 56 days) |

### What works with local models

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

## Offline Capability

Once data is downloaded, the entire stack works without internet:

| Component | Offline? | Notes |
|---|---|---|
| LLM inference (Ollama) | Yes | Models run locally on GPU/CPU |
| Chat UI (Open WebUI) | Yes | Served locally |
| Map viewer | Yes | All tiles served from Martin |
| Map search | Yes | Place index + Photon geocoding, all local |
| Geocoding (Photon) | Yes | OSM database runs locally |
| Wikipedia (Kiwix) | Yes | ZIM files served locally |
| API gateway (LiteLLM) | Partial | Works for local models, fails for cloud APIs |
| Web search (SearXNG) | No | Returns empty results gracefully, nothing breaks |

Nothing crashes without internet. SearXNG returns no results and the chat proceeds without web context. Everything else is fully local.

## How It Works

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

## Filters

What struck me about local models is how much they forget. They lose track of plans. They ignore constraints. They don't know what day it is. These filters compensate for that.

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

## Scripts

All commands are run through the `./krull` wrapper at the project root:

| Command | What it does |
|---|---|
| `./krull start` | Checks dependencies (with OS-specific install hints), pulls images, starts everything |
| `./krull stop` | Stops all services. Your data stays. |
| `./krull setup` | Installs filters into Open WebUI, installs `krull-claude`. Run once after first install. |
| `./krull update` | Pulls latest images and recreates containers. Your data stays. |
| `./krull pull-model` | Pulls Ollama models with tuned parameters (temp 0.7, top_p 0.8) |
| `./krull download-wikipedia` | Downloads Wikipedia ZIM files for Kiwix |
| `./krull download-knowledge` | Downloads dev docs, Stack Exchange, Arch Wiki, and more |
| `./krull download-maps` | Downloads all map types for a region (base + terrain + nautical + aero) |

Individual scripts in `scripts/` still work directly if needed.

## Data and Persistence

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

## GPU

Ollama uses your NVIDIA GPU for inference. You need `nvidia-container-toolkit` on the host. The start script checks for this and tells you how to install it.

Without a GPU, Ollama falls back to CPU. It works. It's just slow.

## Troubleshooting

**Services won't start:** Run `./krull start`. It checks everything and tells you what's missing.

**Model not responding:** Pull at least one model first: `docker exec krull-ollama ollama pull frob/qwen3.5-instruct:9b`

**Claude Code can't connect:** Check that LiteLLM is running (`docker logs krull-litellm`) and that your model name in `litellm/config.yaml` matches what Claude Code expects. Use `ANTHROPIC_AUTH_TOKEN` (not `ANTHROPIC_API_KEY`) to skip the login screen.

**"No connected db" error:** Make sure `litellm/config.yaml` has `general_settings.allow_requests_on_db_unavailable: true`. This is required because LiteLLM runs without a database in this stack.

**Filters not working:** Run `./krull setup` again. Check **Admin Panel > Functions** in Open WebUI to verify they're listed and enabled.

**Out of GPU memory:** Try a smaller model (`frob/qwen3.5-instruct:4b`) or a quantized variant like `frob/qwen3.5-instruct:9b-q4_K_M`.

**FAA chart edition expired:** The FAA updates VFR charts every 56 days. If downloads fail, update the edition date: `FAA_EDITION=MM-DD-YYYY ./krull download-maps --aeronautical pacific-nw`

**Starting fresh:** `docker compose down` stops everything. Data in `data/` is preserved. To truly wipe: delete the `data/` directory and run `./krull start` again.
