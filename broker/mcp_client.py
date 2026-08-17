"""
MCP client to the host playwright-mcp instance (C1/C4).

The broker reaches the operator's own playwright-mcp over its no-auth loopback
Streamable-HTTP transport (the LOCAL trust tier — session_* handoff tools are
available there; see playwright-mcp src/index.ts SurfaceTrust). This module
wraps two operations the broker needs:

  fetch_page(url)  -> read a page's readable text, transparently clearing a
                      CAPTCHA / consent / bot wall via session_solve_challenge
                      (a headed window on the host screen) and retrying once.
  probe()          -> initialize + confirm web_fetch is exposed (health).

A fresh MCP session is opened per operation. The upstream chromium is shared and
long-lived on the host, so re-initializing the MCP session is cheap (no browser
relaunch) and avoids holding a socket open across the broker's idle time.
"""

import hashlib
import json
import logging

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

log = logging.getLogger("broker.mcp")

# web_fetch statuses a headed session_solve_challenge can plausibly clear. A
# paywall or login-wall cannot be solved by clicking through a challenge, and
# 404 / parked have nothing to clear — those get an honest marker instead.
_SOLVABLE = {"blocked", "consent-wall"}


class McpUnavailable(RuntimeError):
    """The playwright-mcp host could not be reached or spoke an unexpected shape."""


def _session_name(url: str) -> str:
    return "broker-" + hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]


def _parse_web_fetch(result) -> dict:
    """web_fetch returns one text content block holding a JSON object. Pull it
    out defensively; raise McpUnavailable if the shape is not what we expect."""
    if getattr(result, "isError", False):
        text = _first_text(result)
        raise McpUnavailable(f"web_fetch error: {text[:200]}")
    text = _first_text(result)
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError) as e:
        raise McpUnavailable(f"web_fetch returned non-JSON: {text[:120]}") from e


def _first_text(result) -> str:
    for block in getattr(result, "content", []) or []:
        if getattr(block, "type", None) == "text":
            return getattr(block, "text", "") or ""
    return ""


class PlaywrightMcp:
    def __init__(self, url: str, solve_enabled: bool = True, solve_timeout_ms: int = 90_000):
        self._url = url
        self._solve_enabled = solve_enabled
        self._solve_timeout_ms = solve_timeout_ms
        # /health must fail fast when the host instance is down (e.g. the ufw
        # allowance is not yet in place): a dropped SYN otherwise burns the SDK's
        # 30s default. fetch_page keeps the longer default so real page reads and
        # the SSE-streamed solve result have room.
        self._probe_timeout = 5.0

    async def probe(self) -> dict:
        """Health check: initialize and confirm web_fetch is exposed."""
        try:
            async with streamablehttp_client(self._url, timeout=self._probe_timeout) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    names = {t.name for t in tools.tools}
                    return {
                        "reachable": True,
                        "web_fetch": "web_fetch" in names,
                        "solve_challenge": "session_solve_challenge" in names,
                        "tool_count": len(names),
                    }
        except Exception as e:  # noqa: BLE001 — health must never raise
            return {"reachable": False, "error": repr(e)[:200]}

    async def fetch_page(self, url: str, allow_solve: bool = True) -> dict:
        """Return {fetch_status, text, citation, solved, solve_attempted}. On a
        solvable wall, when `allow_solve` (and instance solving) is on, opens a
        headed solve window and retries once. `solve_attempted` lets the caller
        distinguish a still-walled page after a real solve attempt (→ honest
        "CAPTCHA not solved" marker) from one never offered a solve. Raises
        McpUnavailable if the host is unreachable — the caller turns that into an
        honest marker.

        `allow_solve=False` is used by bulk enrichment (exhaustive deepening),
        which must not block on headed human interaction — a chat turn cannot
        wait on N sequential 90s solve windows."""
        try:
            async with streamablehttp_client(self._url) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    data = _parse_web_fetch(await session.call_tool("web_fetch", {"url": url}))
                    status = data.get("fetchStatus", "ok")
                    solved = False
                    solve_attempted = False

                    if status in _SOLVABLE and self._solve_enabled and allow_solve:
                        name = _session_name(url)
                        solve_attempted = True
                        log.info("fetch_page: %s wall on %s — opening headed solve window", status, url)
                        try:
                            await session.call_tool(
                                "session_solve_challenge",
                                {"name": name, "url": url, "timeoutMs": self._solve_timeout_ms},
                            )
                            data = _parse_web_fetch(
                                await session.call_tool("web_fetch", {"url": url, "session": name})
                            )
                            status = data.get("fetchStatus", "ok")
                            solved = status == "ok"
                        except Exception as e:  # noqa: BLE001 — solve is best-effort
                            log.warning("fetch_page: solve_challenge failed for %s: %r", url, e)

                    return {
                        "fetch_status": status,
                        "text": _clean_text(data.get("text", "")),
                        "citation": data.get("citation") or {},
                        "solved": solved,
                        "solve_attempted": solve_attempted,
                    }
        except McpUnavailable:
            raise
        except Exception as e:  # transport / init failure
            raise McpUnavailable(repr(e)[:200]) from e


def _clean_text(text: str) -> str:
    """web_fetch wraps page text in <untrusted-content ...>…</untrusted-content>.
    Strip only the outer wrapper tags — the untrusted framing is re-applied by
    OWUI when it injects the document, and the wrapper markup is noise to the
    model otherwise. Content between the tags is preserved verbatim."""
    if not text:
        return ""
    open_end = text.find(">")
    if text.lstrip().startswith("<untrusted-content") and open_end != -1:
        inner = text[open_end + 1 :]
        close = inner.rfind("</untrusted-content>")
        if close != -1:
            inner = inner[:close]
        return inner.strip()
    return text
