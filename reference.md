# KRULL AI Reference

## Open Issues

### ISSUE-001: Filters + Tool Calling for Claude Code

**Status:** RESOLVED (2026-04-04)
**Resolution:** SSE proxy implements filters natively and routes to Ollama directly, preserving both filter functionality and tool_calls.

**Root cause:** Open WebUI's `/api/chat/completions` endpoint swallows `tool_calls` from model responses — it handles them internally instead of passing them through to the API consumer. This made it impossible to route through Open WebUI while maintaining Claude Code's tool calling.

**Solution architecture:**
```
Claude Code → LiteLLM (port 4000) → SSE Proxy (port 8081) → Ollama (port 11434)
                                      ↓
                              Applies inlet filters:
                              - Date/time injection
                              - SearXNG web search (non-tool requests only)
                              - Kiwix offline knowledge (non-tool requests only)
                                      ↓
                              Translates Responses API → Chat Completions
                              (LiteLLM sends Responses API format for
                               Anthropic→OpenAI translation)
```

**Key decisions:**
- Web search and Kiwix filters are skipped when tools are present, because injected search context interferes with tool calling behavior (model responds with text instead of using tools)
- Date filter always runs (lightweight, doesn't interfere)
- Proxy authenticates with Open WebUI on startup for requests that go through WebUI (currently unused but preserved for future)

## Failed Approaches

### 1. Route LiteLLM → Open WebUI `/api/chat/completions` (2026-04-04)
- **What:** Configured LiteLLM with `openai/` provider, pointing at Open WebUI via SSE proxy
- **Why it failed:** LiteLLM v1.82.3 converts Anthropic messages to OpenAI **Responses API** format (POST `/responses`), not Chat Completions. Open WebUI doesn't support the Responses API endpoint (returns 405). The `disable_responses_api` setting doesn't exist in this version.
- **Secondary issue:** Even after translating Responses→ChatCompletions in the proxy, Open WebUI doesn't return `tool_calls` in its API responses — it processes them internally.

### 2. Route LiteLLM → Open WebUI Ollama proxy `/ollama/api/chat` (2026-04-04)
- **What:** Considered using Open WebUI's Ollama proxy endpoint
- **Why it was rejected:** The `/ollama/api/chat` endpoint does NOT run inlet filters. Tested by asking "What day is today?" — the model couldn't answer (no date filter), while `/api/chat/completions` correctly returned today's date with web search references.

## Verified Working Configurations

### Current (2026-04-04): Proxy with native filters → Ollama
- LiteLLM config: `openai/` provider → `http://krull-sse-proxy:8081`
- SSE Proxy: Implements date, web search, kiwix filters natively. Routes to Ollama `/v1/chat/completions`
- Ollama's OpenAI-compatible endpoint preserves `tool_calls` in responses
- **Verified working:** Date awareness, web search enrichment, file reading (Read tool), streaming

### Previous: Direct Ollama routing (bypasses filters)
- LiteLLM config: `ollama_chat/` provider → `http://krull-ollama:11434`
- Tool calling worked but no filters applied
- This is a routing violation per project directives

## Discoveries

- **LiteLLM v1.82.3 Responses API:** When receiving Anthropic format on `/v1/messages` and backend is `openai/`, LiteLLM automatically uses the Responses API (`POST /responses`) instead of Chat Completions. No config option to disable this.
- **Open WebUI tool_calls swallowing:** Open WebUI's `/api/chat/completions` processes tool calls internally and returns text content. The `tool_calls` field is always absent from API responses even when the model generates them.
- **Open WebUI `/ollama/api/chat`:** This endpoint is a direct proxy to Ollama — it does NOT run inlet/outlet filters. Only `/api/chat/completions` runs the filter pipeline.
- **Ollama `/v1/chat/completions`:** Ollama's OpenAI-compatible endpoint fully supports `tool_calls` in responses and works correctly with the Chat Completions tool format.
- **Filter + tool conflict:** Web search context injection causes models to respond with text about tool calling rather than actually using tools. Solution: skip web search for tool-bearing requests.
- **Open WebUI tool_calls bug is tracked upstream:** [open-webui#21557](https://github.com/open-webui/open-webui/issues/21557) (Feb 2026) documents the issue. A fix was proposed in [PR #21555](https://github.com/open-webui/open-webui/pull/21555). If merged, the proxy could be simplified to just Responses→ChatCompletions translation and route through Open WebUI again.

### Model-server evaluation: colibri + GLM-5.2 vs. small local models (2026-08-14)

Evaluated `/home/francis/lab/colibri` (JustVugg/colibri, third-party read-only) as a possible Ollama replacement.

- **colibri is not a general model runner.** It runs only 5 compiled-in frontier MoE families (GLM-5.2 744B, Inkling 975B, Kimi K3 2.8T, DeepSeek V4 Flash 284B, OLMoE 7B), one C file each. It cannot load Krull's `gemma4`/`qwen3.5` models, and it downloads safetensors from HuggingFace + converts (`coli convert`) — a different ecosystem from the Ollama registry. **Conclusion: keep Ollama** for both serving the small interactive model and downloading; colibri cannot subsume either role.
- **Tool-calling in colibri is GLM-only** (`docs/api.md`): Inkling/Kimi/DeepSeek reject tool requests. So GLM-5.2 is the only colibri model that can drive an agent.
- **GLM-5.2 the model is excellent** (62.1 SWE-bench Pro, ~Opus-4.8-no-thinking on agent arena, day-one Claude Code support) — quality is NOT the blocker. **Throughput on local hardware is.**
- **Measured hardware (this host, RTX 5000 eGPU):** Quadro RTX 5000 16 GB VRAM, 31.7 GB RAM, i7-10510U (PCIe **3.0** → caps NVMe ~3.5 GB/s AND the Thunderbolt eGPU link), 1.8 TB NVMe / 655 GB free. Meets colibri's GLM-5.2 minimum (16 GB VRAM, ~25 GB RAM, 372 GB fits) but only ~6-8% of experts stay resident → ~90% stream from NVMe/token. Estimated **~0.3-1 tok/s** (matches the 1.07 tok/s single-laptop-GPU reference), with ~2-hour first-turn prefill of the Claude Code system prompt. = overnight batch, not interactive.
- **Recommended path instead: upgrade the model in the existing Ollama slot.** `Gemma 4 12B` (in-family upgrade from current `gemma4:e4b`) is the standout for this 16 GB box: Q4 weights ~6.6-8 GB, and its interleaved sliding-window (1024-tok cap) + shared-KV architecture keeps context cheap → **~128K usable context on 16 GB** (matches current `OLLAMA_NUM_CTX=131072`), at **~15-25 tok/s** on the RTX 5000 (decode is on-card, so the Thunderbolt link doesn't bottleneck a fully-resident model). Native tool calling with strong output-format reliability. Alt: `Devstral Small 24B` (68% SWE-bench Verified, best agentic muscle) but dense → context-hungry (~32K on 16 GB) and runs at the memory edge. Honest ceiling: ~29-pt SWE-bench gap to hosted Opus — scope local agentic tasks tightly.
- Sources: colibri repo docs; remio.ai "Storage Sets the Pace"; interconnects.ai (Lambert); blog.kilo.ai + genalphai.com 16 GB rankings; HuggingFace Gemma 4 blog; vLLM recipe.

### claude-workforce agents on a single Krull model — generic model remap (2026-08-14)

Reviewed `/home/francis/lab/claude-workforce` (odysseyalive/claude-workforce, user's own). Its agents (`.claude/agents/*.md`) pin a `model:` in frontmatter; `org-config.template.md` allows only `claude-*` cloud IDs (`claude-fable-5`, `claude-opus-5`, `claude-opus-4-8`, `claude-opus-4-6`, plus hand-typed "Other"). Live agents pin `claude-opus-4-8` / `claude-opus-5`.

- **Problem:** those IDs are NOT in `litellm/config.yaml` (which only maps sonnet-4-6, opus-4-6, opus-4-20250514, haiku-4-5*). A workforce subagent pinned to `claude-opus-5` **400s at LiteLLM before reaching the proxy** ("model not found"). Path: `Claude Code → LiteLLM :4000 → sse-proxy :8081 → Ollama`.
- **Fix (generic, no skill/agent edits):** add ONE LiteLLM wildcard catch-all — `- model_name: "*"` → `model: openai/gemma4:12b` @ `api_base http://krull-sse-proxy:8081`. LiteLLM's documented wildcard-routing feature (docs.litellm.ai/docs/wildcard_routing). Matches ANY agent-pinned ID (incl. future/hand-typed ones), routes to the single active Krull model. Explicit entries still match first; wildcard is fallback. `start.sh`'s sed (`model: openai/… → active OLLAMA_MODEL`) keeps the wildcard target in sync. NOT a bypass (same proxy api_base as every entry); NOT a proxy-code change (Structural-Rationale Gate for proxy/** doesn't fire). A proxy-side normalization is unnecessary and would require plumbing OLLAMA_MODEL into the proxy env (currently absent).
  - Rejected alternative: enumerating explicit `claude-opus-4-8`/`claude-opus-5` entries — case-shaped, rots when workforce adds a model.
- **ROUTING GUARD note:** implement.md step 16 says api_base must be `krull-webui`, but the ratified live config routes all entries to `krull-sse-proxy:8081` (proxy replaces the WebUI filter pipeline). Binding intent = never `krull-ollama` / never bypass filters; wildcard mirrors existing entries, so honored.
- **"Add gemma4:12b alongside the other two" = Krull, not workforce.** "This package" resolves to krull-home's `RECOMMENDED_MODELS` (`krull-home/server/lib/models.ts`); "the other two" = `gemma4:e2b` + `gemma4:e4b`. Add a third data-only entry (key `gemma4:12b`, ~8 GB weights resident, native 128K ctx) → first-class one-click download/tune in the picker. NOT workforce: its model-map only proposes `claude-*` cloud IDs and has no download concept.
- Caveat: `gemma4:12b` is a thinking model, so every remapped agent inherits a reasoning pass — watch interaction with the proxy's grounded-answer/tool-call heuristics under multi-agent load.
- **IMPLEMENTED + verified 2026-08-14.** Added the `model_name: "*"` wildcard to `litellm/config.yaml` (routing guard: 7/7 entries via krull-sse-proxy, 0 via krull-ollama) and a `gemma4:12b` entry to krull-home `RECOMMENDED_MODELS` (tsc --noEmit clean). Verified through LiteLLM :4000: `claude-opus-5`, `claude-opus-4-8`, and an arbitrary `totally-made-up-model-xyz` all resolve to gemma4:12b and return content (prompt_tokens ~1426 → proxy filters ran); explicit `claude-sonnet-4-6` still matches its named route first (regression clean). 4 consecutive successful runs. NOTE: the krull-home picker surfaces gemma4:12b only after the krull-home container is rebuilt; the wildcard remap is live immediately.

### playwright-mcp bootstrap in Krull setup (2026-08-14)

Added an idempotent one-time bootstrap of `odysseyalive/playwright-mcp` to `scripts/setup.sh` (after the krull-claude install, before the `.setup-complete` sentinel). Krull uses that MCP server for browser automation (web_fetch w/ citations, browser_* debugging, session_login).

- **Detection guard (robustness trap):** `krull-ai/.mcp.json` is committed with a machine-specific path (`/home/francis/lab/playwright-mcp/dist/index.js`), so a bare "is it registered?" check falsely passes on a fresh clone. Guard therefore checks **registered AND target file exists**: `PW_TARGET=$(claude mcp get playwright-mcp | grep -oE '/[^ ]+/dist/index.js' | head -1); [ -n "$PW_TARGET" ] && [ -f "$PW_TARGET" ]`. Present → skip untouched (user's "never update" constraint); absent → install.
- **Install:** clone via HTTPS to the sibling `$(dirname "$PROJECT_DIR")/playwright-mcp` only if absent, then delegate to the project's own re-runnable `./install.sh` (full deny+steer footprint, per user choice). No reimplementation of its build/Chromium/register logic.
- **`set -e` safety:** setup.sh runs under `set -e`; every fallible step (clone, install.sh) is `|| echo`-guarded so a partial failure can't abort setup before the sentinel. Prereqs (git/node/npm/claude) checked; missing → skip with manual instructions.
- **Idempotent at two levels:** the sentinel stops setup re-running on normal boots; the guard makes `./krull setup` / `./krull update` re-runs a no-op when present. Verified: `bash -n` clean; guard simulated on this machine → SKIP (no clone/install).
