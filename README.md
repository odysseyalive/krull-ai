# Krull AI

A self-hosted AI stack that lets you code, search the web, and access offline knowledge — all running on your own hardware. No cloud dependencies, no API keys, no internet required after setup.

Krull AI is designed to work as a local backend for [Claude Code](https://claude.ai/code), but also works standalone through its browser-based chat UI.

## What's Included

| Service | What it does | URL |
|---|---|---|
| **Open WebUI** | Browser-based chat interface with RAG, tools, and pipelines | <http://localhost:3000> |
| **Ollama** | Runs LLM models on your GPU | internal |
| **LiteLLM** | API gateway so Claude Code can talk to your local models | <http://localhost:4000> |
| **SearXNG** | Web search aggregator (Google, DuckDuckGo, Brave) — no API keys needed | internal |
| **Kiwix** | Offline Wikipedia and knowledge base | <http://localhost:8090> |

## Prerequisites

- **Docker** and **Docker Compose** (v2 plugin)
- **curl**
- **NVIDIA GPU** with `nvidia-container-toolkit` (optional — CPU works but is slow)

Don't worry about remembering these — the start script checks for everything and tells you exactly what to install for your OS (Arch, Ubuntu, Fedora, macOS, etc.).

## Installation

### 1. Clone and start

```bash
git clone <repo-url> krull-ai
cd krull-ai
./scripts/start.sh
```

The first run will:

- Verify all dependencies are installed
- Pull Docker images (~10 GB total)
- Start all five services
- Report the status of each container

### 2. Pull a model

You need at least one LLM model. Pick one based on your GPU memory:

| Model | VRAM | Best for |
|---|---|---|
| `qwen2.5:3b` | ~3 GB | Quick responses, low-end hardware |
| `qwen2.5-coder:7b` | ~6 GB | Coding with larger context |
| `qwen2.5-coder:14b` | ~12 GB | Best coding quality |

Pull it through the CLI:

```bash
docker exec krull-ollama ollama pull qwen2.5:3b
```

Or through the Open WebUI browser interface at <http://localhost:3000> under **Settings > Models**.

### 3. Provision the filters

```bash
./scripts/setup.sh
```

This installs intelligent filters into Open WebUI that enhance your local model's capabilities. Run this once after first install. You can run it again after updates to re-provision.

### 4. (Optional) Download offline knowledge

A small Wikipedia mini (~5 MB) is downloaded automatically on first run. You can add more:

**Wikipedia:**

```bash
./scripts/download-wikipedia.sh medicine    # Medical articles (~2 GB)
./scripts/download-wikipedia.sh nopic       # All articles, no images (~25 GB)
./scripts/download-wikipedia.sh full        # Everything with images (~115 GB)
```

**Developer docs, Stack Exchange, and more:**

```bash
./scripts/download-knowledge.sh dev-essentials      # Python, JS, TS, Node, Git, Docker, Bash (~50 MB)
./scripts/download-knowledge.sh web-dev              # JS, TS, React, CSS, HTML, Node (~25 MB)
./scripts/download-knowledge.sh data-science         # Python, NumPy, Pandas, Scikit-learn (~75 MB)
./scripts/download-knowledge.sh sysadmin             # Arch Wiki, Unix & Server Q&A (~5.5 GB)
./scripts/download-knowledge.sh community            # Unix, Code Review, Security, SoftEng Q&A (~5 GB)
```

Or pick individual packages:

```bash
./scripts/download-knowledge.sh devdocs-python devdocs-rust archlinux
```

Run `./scripts/download-knowledge.sh` with no arguments to see the full catalog.

After downloading, restart Kiwix to load new content:

```bash
docker restart krull-kiwix
```

### 5. Start chatting

Open <http://localhost:3000> in your browser. Select your model and start a conversation.

## Using with Claude Code

This is the main use case — running Claude Code against your local models so you can keep coding without internet access.

### Connect Claude Code to your local stack

```bash
export ANTHROPIC_BASE_URL=http://localhost:4000
export ANTHROPIC_API_KEY=sk-local-dev-key
claude
```

### Configure model mapping

Claude Code sends requests using Anthropic model names. LiteLLM maps these to your local models. Edit `litellm/config.yaml`:

```yaml
model_list:
  - model_name: claude-sonnet-4-20250514
    litellm_params:
      model: openai/qwen2.5-coder:14b
      api_base: http://krull-webui:8080
      api_key: "none"
```

Map as many Claude model names as you want. The `openai/` prefix tells LiteLLM to use the OpenAI-compatible API format that Open WebUI provides.

### What works with local models

| Feature | Status | Notes |
|---|---|---|
| Code generation | Works well | Quality depends on model size |
| File read/write/edit | Works | Standard tool calling |
| Bash commands | Works | Standard tool calling |
| Git operations | Works | Standard tool calling |
| Skills (`/commit`, `/simplify`, etc.) | Works | Skill Adapter filter helps smaller models |
| Plan mode (`/plan`) | Works | Plan Mode Assistant filter provides guardrails |
| Plan execution | Works | Plan Execution Tracker keeps model on task |
| Web search context | Works | Auto Web Search filter injects results |
| Offline knowledge | Works | Kiwix Lookup filter injects articles |
| Context management | Works | Context Manager auto-compacts long conversations |
| Hooks | Works | Hooks run in the Claude Code CLI, not the model |
| Extended thinking | Limited | Depends on model capability |
| Multi-agent coordination | Limited | Smaller models struggle with parallel agents |

## How It Works

```
                           ┌──────────────────────────────────────────┐
                           │              Open WebUI                  │
  Browser ────────────────►│  (filters, pipelines, RAG, web search)  │──► Ollama ──► GPU
                           └──────────────────────────────────────────┘
                                            ▲
  Claude Code ──► LiteLLM (port 4000) ──────┘
                  (translates API format)
```

**Browser path:** You chat directly in Open WebUI at <http://localhost:3000>. Filters, RAG, and web search are built in.

**Claude Code path:** Claude Code speaks Anthropic API format. LiteLLM translates it to OpenAI format and routes it through Open WebUI. This means your local model gets the same filters, tools, and knowledge that the browser UI provides.

**Both paths** go through Open WebUI's filter pipeline, so any tools or knowledge sources you configure apply everywhere.

## Filters

Filters are Python functions that process every request before it reaches the model. They run inside Open WebUI and apply to both browser and API traffic. The `setup.sh` script installs these globally:

| Filter | What it does |
|---|---|
| **Context Manager** | Monitors token usage and automatically summarizes older messages when approaching the context limit. Keeps long conversations alive instead of crashing. |
| **Auto Web Search** | Searches SearXNG before every response and injects the top results into context, giving the model current information. |
| **Kiwix Knowledge Lookup** | Searches the offline knowledge base and injects relevant article snippets. Works without internet. |
| **Plan Mode Assistant** | Detects when Claude Code enters plan mode and guides the model through each phase (explore, design, review, write plan). Reinforces read-only constraints. |
| **Plan Execution Tracker** | After a plan is approved, tracks which steps are done and injects progress reminders so the model stays focused during implementation. |
| **Skill Adapter** | Parses any Claude Code skill on the fly — extracts directives, workflow steps, and constraints, then presents them in a format smaller models can follow. |

All filter settings are adjustable in Open WebUI: **Admin Panel > Functions > [filter name] > Valves**.

Key settings you may want to tune:

| Setting | Filter | Default | What to change |
|---|---|---|---|
| `max_context_tokens` | Context Manager | 16384 | Match your model's context window |
| `compact_threshold` | Context Manager | 0.75 | Lower = more aggressive compaction |
| `num_results` | Auto Web Search | 5 | Fewer results = less context used |
| `enabled` | Any filter | true | Disable filters you don't need |

## Scripts Reference

| Script | What it does |
|---|---|
| `scripts/start.sh` | Checks dependencies (with OS-specific install hints), pulls Docker images, starts all services, reports status |
| `scripts/setup.sh` | Creates an admin account in Open WebUI, installs all filters, enables them globally. Run once after first install. |
| `scripts/stop.sh` | Stops all services. Data is preserved. |
| `scripts/update.sh` | Pulls latest Docker images and recreates containers. Data is preserved. |
| `scripts/download-wikipedia.sh` | Downloads Wikipedia ZIM files for offline access via Kiwix. |
| `scripts/download-knowledge.sh` | Downloads developer docs, Stack Exchange, Arch Wiki, and other knowledge bases. Run with no args to see the full catalog. |

## Data and Persistence

All data lives in local directories within this project — not in Docker's internal storage. You can rebuild containers, update images, or change the compose file without losing anything.

| Directory | What's stored there |
|---|---|
| `data/ollama/` | Downloaded LLM models |
| `data/webui/` | Chat history, documents, settings, RAG embeddings |
| `functions/` | Inlet filter source code (Python) |
| `litellm/` | LiteLLM model mapping configuration |
| `searxng/` | SearXNG search engine configuration |
| `zim/` | Kiwix ZIM files (offline Wikipedia, etc.) |

## GPU Setup

Ollama uses your NVIDIA GPU for inference. This requires `nvidia-container-toolkit` on the host. The start script checks for this and provides install instructions if it's missing.

Without a GPU, Ollama falls back to CPU — it works but is significantly slower.

## Troubleshooting

**Services won't start:** Run `./scripts/start.sh` — it checks all dependencies and tells you what's missing.

**Model not responding:** Make sure you've pulled at least one model: `docker exec krull-ollama ollama pull qwen2.5-coder:14b`

**Claude Code can't connect:** Verify LiteLLM is running (`docker logs krull-litellm`) and that your model name in `litellm/config.yaml` matches what Claude Code sends.

**Filters not working:** Run `./scripts/setup.sh` again to re-provision. Check **Admin Panel > Functions** in Open WebUI to verify they're enabled.

**Out of GPU memory:** Use a smaller model or a quantized variant (e.g., `qwen2.5-coder:7b-instruct-q4_K_M` instead of the full 14b).

**Need to start fresh:** `docker compose down` stops everything. Data in `data/` is preserved. To wipe everything: delete the `data/` directory and run `start.sh` again.
