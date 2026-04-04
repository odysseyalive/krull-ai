# KRULL AI

**Knowledge, Reasoning, and Unified Local Learning**

*Named after the [1983 film](https://en.wikipedia.org/wiki/Krull_(film)) where a band of unlikely allies sets out on a quest armed with a single, powerful tool. This project does something similar. It assembles a team of open-source tools, each with its own specialty, and points them at a single goal: keeping you productive when your internet goes dark.*

A self-hosted AI stack that runs entirely on your own hardware. Local inference, web search, offline knowledge, geocoding, maps, and an API gateway that lets [Claude Code](https://claude.ai/code) talk to your local models. No cloud dependencies. No API keys. No internet required after setup.

## What's Inside

| Service | What it does | URL |
|---|---|---|
| **Open WebUI** | Browser-based chat with RAG, tools, and pipelines | <http://localhost:3000> |
| **Ollama** | GPU-accelerated LLM inference | internal |
| **LiteLLM** | API gateway so Claude Code can talk to local models | <http://localhost:4000> |
| **SearXNG** | Web search aggregator (Google, DuckDuckGo, Brave) | internal |
| **Martin** | Offline map tiles (OpenStreetMap via PMTiles) | <http://localhost:8070> |
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

### 5. (Optional) Build your offline library

The thing is, the real power here shows up when the internet isn't available. Stock up while you can.

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

Run `./krull download-knowledge` with no arguments to see the full catalog.

**Offline maps:**

```bash
./krull download-maps oregon      # ~100 MB (extracted via HTTP range requests)
./krull download-maps us-west     # ~800 MB
./krull download-maps us          # ~3 GB
docker restart krull-tileserver
```

Photon geocoding data (place search) downloads automatically on first start.

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

Claude Code sends requests using Anthropic model names. LiteLLM maps them to your local models. Edit `litellm/config.yaml`:

```yaml
general_settings:
  allow_requests_on_db_unavailable: true

litellm_settings:
  drop_params: true

model_list:
  - model_name: claude-sonnet-4-6
    litellm_params:
      model: ollama_chat/frob/qwen3.5-instruct:9b
      api_base: http://krull-ollama:11434    # Docker internal URL (not localhost)
```

> **Note:** The `ollama_chat/` provider is required for tool calling — it translates between Anthropic's `tool_use` format and Ollama's format. The `openai/` provider does not support this. `drop_params: true` silently drops Anthropic-specific parameters (like `cache_control`) that Ollama doesn't support. `allow_requests_on_db_unavailable` prevents "No connected db" errors since we run without a database.

### What works with local models

| Feature | Status | Notes |
|---|---|---|
| Code generation | Works well | Quality scales with model size |
| File read/write/edit | Works | Standard tool calling |
| Bash commands | Works | Standard tool calling |
| Git operations | Works | Standard tool calling |
| Skills (`/commit`, `/simplify`, etc.) | Works | Skill Adapter filter helps smaller models follow along |
| Plan mode (`/plan`) | Works | Plan Mode Assistant provides phase-by-phase guardrails |
| Plan execution | Works | Plan Execution Tracker keeps the model on task |
| Web search context | Works | Auto Web Search injects results before every response |
| Offline knowledge | Works | Kiwix Lookup injects relevant articles |
| Map/location queries | Works | Offline Map Search finds places and coordinates |
| Context management | Works | Context Manager auto-compacts long conversations |
| Hooks | Works | Hooks run in the Claude Code CLI, independent of the model |
| Extended thinking | Limited | Depends on model capability |
| Multi-agent coordination | Limited | Smaller models struggle with parallel agents |

## How It Works

```
                           ┌──────────────────────────────────────────┐
                           │              Open WebUI                  │
  Browser ────────────────►│  (filters, pipelines, RAG, web search)  │──► Ollama ──► GPU
                           └──────────────────────────────────────────┘        ▲
                                                                               │
                           ┌──────────────────────────────────────────┐        │
                           │            SSE Proxy (port 4001)         │────────┘
  Claude Code ──► LiteLLM ►│  (web search, kiwix, date injection,    │◄── SearXNG
                 (port 4000)│   context compaction, SSE streaming)    │◄── Kiwix
                            └──────────────────────────────────────────┘
```

Two paths to the same brain. The browser talks to Open WebUI directly, where Python filters handle context enrichment. Claude Code goes through LiteLLM, which translates the Anthropic API format and routes to the SSE proxy. The proxy injects the same capabilities — web search, offline knowledge, date/time, context compaction — then streams the response back as Anthropic-compatible SSE events.

## Filters

What struck me about local models is how much they forget. They lose track of plans. They ignore constraints. They don't know what day it is. These filters compensate for that.

They're Python functions that process every request before it reaches the model. The `setup.sh` script installs them globally in Open WebUI:

| Filter | What it does |
|---|---|
| **Current Date & Time** | Injects today's date and current time into every request. Local models have no sense of time otherwise. |
| **Context Manager** | Monitors token usage and auto-compacts older messages when the conversation approaches the context limit. Keeps things alive instead of crashing. |
| **Auto Web Search** | Queries SearXNG before every response and injects the top results with source URLs. The model cites what it finds. |
| **Kiwix Knowledge Lookup** | Searches the offline knowledge base and injects relevant article snippets. Works without internet. |
| **Plan Mode Assistant** | Detects when Claude Code enters plan mode and walks the model through each phase. Reinforces the "don't edit anything" constraint that smaller models love to ignore. |
| **Plan Execution Tracker** | After a plan is approved, tracks which steps are done and injects progress reminders. "You are on step 3 of 7. Stay focused." |
| **Skill Adapter** | Parses any Claude Code skill on the fly, extracts the directives and workflow steps, and presents them in a format smaller models can actually follow. |
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
| `./krull download-maps` | Downloads OpenStreetMap tiles (PMTiles via Protomaps) for offline maps |

Individual scripts in `scripts/` still work directly if needed.

## Data and Persistence

Everything lives in local directories. Not Docker volumes. Not ephemeral storage. You can tear down containers, update images, change the compose file. Your models, chat history, maps, and knowledge stay right where they are.

| Directory | What's there |
|---|---|
| `data/ollama/` | Downloaded LLM models |
| `data/webui/` | Chat history, documents, settings, RAG embeddings |
| `data/tiles/` | OpenStreetMap tile data (PMTiles format) |
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

**Starting fresh:** `docker compose down` stops everything. Data in `data/` is preserved. To truly wipe: delete the `data/` directory and run `./krull start` again.
