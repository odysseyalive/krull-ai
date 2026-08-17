"""
krull-web-broker (C2) — the deterministic web-access broker for the browser/API
chat path (Open WebUI → Ollama gemma4:e2b). Distinct from the Claude Code /
LiteLLM / SSE-proxy path, which this never touches.

It implements the Open WebUI `external` web-search + web-loader HTTP contracts so
the weak local model gets robust, intent-routed web access WITHOUT ever emitting
a tool call, and without bypassing Open WebUI:

  POST /search  {query, count}   -> [{link, title, snippet}]   (external search)
  POST /load    {urls: [...]}    -> [{page_content, metadata}]  (external loader)
  GET  /health                   -> {status, searxng, mcp}

Pipeline for /search:
  1. classify intent on a separate deterministic pass (router.classify)
  2. discover via SearXNG in the intent's category (reliable, in-stack primary)
  3. EXHAUSTIVE additionally deepens: web_fetch the top results and swap thin
     snippets for real page text, clearing walls via the headed CAPTCHA handoff
  4. never return a silent empty list — attach an honest marker naming the reason

Every failure state is distinct and honest (markers.py); the model is never left
to infer "I can't browse" from silence.
"""

import logging
import os

from aiohttp import web

import markers
import router
from mcp_client import McpUnavailable, PlaywrightMcp
from searxng import SearxngError, search as searxng_search

logging.basicConfig(
    level=os.environ.get("BROKER_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("broker")


def _cfg():
    return {
        "searxng_url": os.environ.get("SEARXNG_URL", "http://krull-searxng:8080"),
        "mcp_url": os.environ.get("PLAYWRIGHT_MCP_URL", "http://host.docker.internal:8765/mcp"),
        "default_count": int(os.environ.get("BROKER_RESULT_COUNT", "5")),
        "solve_enabled": os.environ.get("BROKER_SOLVE_ENABLED", "true").lower() == "true",
        "solve_timeout_ms": int(os.environ.get("BROKER_SOLVE_TIMEOUT_MS", "90000")),
        "exhaustive_fetch": int(os.environ.get("BROKER_EXHAUSTIVE_FETCH", "3")),
        "exhaustive_multiplier": int(os.environ.get("BROKER_EXHAUSTIVE_MULTIPLIER", "2")),
        "page_text_cap": int(os.environ.get("BROKER_PAGE_TEXT_CAP", "4000")),
        "api_key": os.environ.get("BROKER_API_KEY", ""),
    }


def _authorized(request: web.Request, cfg: dict) -> bool:
    """Bearer check is opt-in: enforced only when BROKER_API_KEY is set. Traffic
    is loopback within the docker network otherwise."""
    if not cfg["api_key"]:
        return True
    auth = request.headers.get("Authorization", "")
    return auth == f"Bearer {cfg['api_key']}"


def _mcp(cfg: dict) -> PlaywrightMcp:
    return PlaywrightMcp(
        cfg["mcp_url"],
        solve_enabled=cfg["solve_enabled"],
        solve_timeout_ms=cfg["solve_timeout_ms"],
    )


async def _deepen(cfg: dict, results: list) -> list:
    """EXHAUSTIVE: replace the top results' thin SearXNG snippets with real
    fetched page text. Per-URL failures are non-fatal — the original snippet is
    kept, with an honest note when a page could not be read.

    Bulk enrichment never opens headed CAPTCHA-solve windows (allow_solve=False):
    a chat turn must not block on N sequential 90s human-solve prompts. Inline
    solving is reserved for explicit /load. A walled page here just keeps its
    snippet."""
    mcp = _mcp(cfg)
    for r in results[: cfg["exhaustive_fetch"]]:
        url = r.get("link")
        if not url:
            continue
        try:
            page = await mcp.fetch_page(url, allow_solve=False)
        except McpUnavailable as e:
            log.warning("deepen: mcp unavailable for %s: %s", url, e)
            break  # host is down; stop trying, keep remaining snippets as-is
        status = page["fetch_status"]
        if status == "ok" and page["text"]:
            r["snippet"] = page["text"][: cfg["page_text_cap"]].strip()
            log.info("deepen: fetched full text for %s (solved=%s)", url, page["solved"])
        else:
            r["snippet"] = (
                f'{r.get("snippet", "")} '
                f"[full page could not be read: {status}; snippet only]"
            ).strip()
            log.info("deepen: %s not readable (status=%s)", url, status)
    return results


async def handle_search(request: web.Request) -> web.Response:
    cfg = _cfg()
    if not _authorized(request, cfg):
        return web.json_response({"error": "unauthorized"}, status=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    query = (body.get("query") or "").strip()
    try:
        count = int(body.get("count") or cfg["default_count"])
    except (TypeError, ValueError):
        count = cfg["default_count"]
    if not query:
        return web.json_response([])

    intent = router.classify(query)
    category = router.searxng_category(intent)
    discover_count = count * cfg["exhaustive_multiplier"] if intent == router.EXHAUSTIVE else count
    log.info("search: intent=%s category=%s count=%s query=%r", intent, category, discover_count, query)

    try:
        results = await searxng_search(
            cfg["searxng_url"], query, discover_count, category=category, intent=intent
        )
    except SearxngError as e:
        reason = markers.REASON_RATE_LIMITED if "rate-limited" in str(e) else (
            markers.REASON_TIMED_OUT if "timeout" in str(e) else markers.REASON_ENGINE_ERROR
        )
        log.warning("search: searxng failed (%s) -> honest marker %r", e, reason)
        return web.json_response(markers.unavailable_search(query, reason))

    if not results:
        log.info("search: zero results -> honest no-results marker")
        return web.json_response(markers.unavailable_search(query, markers.REASON_NO_RESULTS))

    if intent == router.EXHAUSTIVE:
        results = await _deepen(cfg, results)

    return web.json_response(results[:count])


async def handle_load(request: web.Request) -> web.Response:
    cfg = _cfg()
    if not _authorized(request, cfg):
        return web.json_response({"error": "unauthorized"}, status=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    urls = body.get("urls") or []
    if isinstance(urls, str):
        urls = [urls]
    log.info("load: %d url(s)", len(urls))

    mcp = _mcp(cfg)
    docs = []
    host_down = False
    for url in urls:
        if host_down:
            # The host instance is unreachable; don't reopen a fresh MCP session
            # per remaining URL — one honest marker each, no wasted attempts.
            docs.append(markers.unavailable_document(url, markers.REASON_ENGINE_ERROR))
            continue
        try:
            page = await mcp.fetch_page(url)
        except McpUnavailable as e:
            log.warning("load: mcp unavailable for %s: %s (short-circuiting remaining)", url, e)
            host_down = True
            docs.append(markers.unavailable_document(url, markers.REASON_ENGINE_ERROR))
            continue
        status = page["fetch_status"]
        if status == "ok" and page["text"]:
            docs.append(
                {
                    "page_content": page["text"],
                    "metadata": {
                        "source": url,
                        "title": (page["citation"] or {}).get("title", ""),
                        "krull_fetch_status": "ok",
                    },
                }
            )
        else:
            reason = _load_reason(status, page.get("solve_attempted", False))
            log.info("load: %s not readable (status=%s) -> honest doc marker", url, status)
            docs.append(markers.unavailable_document(url, reason))
    return web.json_response(docs)


def _load_reason(status: str, solve_attempted: bool = False) -> str:
    # A wall that survived a real headed solve attempt is honestly "CAPTCHA not
    # solved in time", distinct from one that was never offered a solve.
    if solve_attempted and status in ("blocked", "consent-wall"):
        return markers.REASON_CAPTCHA_UNSOLVED
    return {
        "ok": markers.REASON_NO_CONTENT,  # fetched fine, but no readable text
        "login-wall": markers.REASON_LOGIN_WALL,
        "paywall": markers.REASON_PAYWALL,
        "blocked": markers.REASON_BLOCKED,
        "consent-wall": markers.REASON_BLOCKED,
    }.get(status, markers.REASON_ENGINE_ERROR)


async def handle_health(request: web.Request) -> web.Response:
    cfg = _cfg()
    mcp_status = await _mcp(cfg).probe()
    return web.json_response(
        {
            "status": "ok",
            "searxng_url": cfg["searxng_url"],
            "mcp": mcp_status,
            "solve_enabled": cfg["solve_enabled"],
        }
    )


def make_app() -> web.Application:
    app = web.Application()
    app.router.add_post("/search", handle_search)
    app.router.add_post("/load", handle_load)
    app.router.add_get("/health", handle_health)
    return app


if __name__ == "__main__":
    port = int(os.environ.get("BROKER_PORT", "8130"))
    log.info("krull-web-broker starting on :%d", port)
    web.run_app(make_app(), host="0.0.0.0", port=port, access_log=None)
