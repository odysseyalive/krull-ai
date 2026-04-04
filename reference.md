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
