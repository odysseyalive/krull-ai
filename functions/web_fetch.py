"""
Open WebUI Inlet Filter: Auto Web Fetch via sse-proxy /fetch
Detects URLs in the latest user message, fetches each one through the
sse-proxy's chromium-rendered /fetch endpoint, and prepends the
rendered content into the message context before the model responds.

Sibling to web_search.py. Shares the same composition model: multiple
inlet filters can prepend their contributions to the same user
message; later filters see earlier injections and stack on top
without clobbering.
"""

import re
import urllib.parse
from pydantic import BaseModel, Field
from typing import Optional


# Liberal URL regex — requires an explicit http/https scheme, a host
# with a dot (to avoid matching localhost-style bare words), and lets
# the path run until whitespace or common punctuation. No attempt to
# parse query strings perfectly — urllib.parse handles that downstream.
_URL_RE = re.compile(
    r"https?://[^\s<>\"')]+",
    re.IGNORECASE,
)

# Hosts we refuse to fetch from user-triggered flows. Localhost/private
# ranges would let a user trick WebUI into scanning internal services.
# The sse-proxy's /fetch endpoint is on localhost but reached only via
# the tool-rewrite path, not user-supplied URLs.
_PRIVATE_HOST_RE = re.compile(
    r"^(?:localhost|127\.|10\.|192\.168\.|172\.(?:1[6-9]|2\d|3[01])\.|::1|fc[0-9a-f]{2}:)",
    re.IGNORECASE,
)


def _extract_urls(text: str, limit: int) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for match in _URL_RE.finditer(text or ""):
        url = match.group(0).rstrip(".,;:!?)]}'\"")
        try:
            parsed = urllib.parse.urlparse(url)
        except ValueError:
            continue
        host = (parsed.hostname or "").lower()
        if not host or "." not in host:
            continue
        if _PRIVATE_HOST_RE.match(host):
            continue
        if url in seen:
            continue
        seen.add(url)
        out.append(url)
        if len(out) >= limit:
            break
    return out


class Filter:
    class Valves(BaseModel):
        priority: int = Field(
            default=3,
            description=(
                "Filter priority (lower runs first). Web fetch runs "
                "after kiwix and web search so URL-referenced content "
                "lands closest to the user's question."
            ),
        )
        proxy_url: str = Field(
            default="http://krull-sse-proxy:8081",
            description="sse-proxy base URL (the /fetch endpoint is appended)",
        )
        max_urls: int = Field(
            default=3, description="Maximum URLs to fetch per message"
        )
        max_chars: int = Field(
            default=40000, description="Per-URL markdown size cap"
        )
        timeout_seconds: float = Field(
            default=20.0, description="Per-URL fetch timeout"
        )
        enabled: bool = Field(
            default=True, description="Enable/disable web fetch injection"
        )

    def __init__(self):
        self.valves = self.Valves()

    async def inlet(self, body: dict, __user__: Optional[dict] = None) -> dict:
        if not self.valves.enabled:
            return body

        messages = body.get("messages", [])
        if not messages:
            return body

        last_message = messages[-1]
        if last_message.get("role") != "user":
            return body

        # Use the user's ORIGINAL query, not whatever previous inlet
        # filters may have prepended. Matches web_search.py's pattern.
        query = body.get("_krull_original_query")
        if query is None:
            query = last_message.get("content", "")
            body["_krull_original_query"] = query
        if not isinstance(query, str) or not query:
            return body

        urls = _extract_urls(query, self.valves.max_urls)
        if not urls:
            return body

        try:
            import aiohttp
        except ImportError:
            return body

        fetched: list[tuple[str, str]] = []
        try:
            async with aiohttp.ClientSession() as session:
                for url in urls:
                    fetch_endpoint = (
                        f"{self.valves.proxy_url.rstrip('/')}/fetch"
                        f"?url={urllib.parse.quote(url, safe='')}"
                        f"&max_chars={self.valves.max_chars}"
                    )
                    try:
                        async with session.get(
                            fetch_endpoint,
                            timeout=aiohttp.ClientTimeout(
                                total=self.valves.timeout_seconds
                            ),
                        ) as resp:
                            if resp.status != 200:
                                continue
                            markdown = await resp.text()
                    except Exception:
                        continue
                    if markdown and markdown.strip():
                        fetched.append((url, markdown))
        except Exception:
            return body

        if not fetched:
            return body

        context_lines = ["[Fetched Web Content]"]
        for url, markdown in fetched:
            context_lines.append(f"--- {url} ---")
            context_lines.append(markdown.strip())
        context_lines.append("[End Fetched Web Content]")
        context_lines.append("")
        context_lines.append(
            "The content above was just fetched live; treat it as "
            "current and cite any details you use from it."
        )
        context_lines.append("")
        fetch_context = "\n".join(context_lines)

        messages[-1]["content"] = (
            f"{fetch_context}\n{messages[-1]['content']}"
        )

        return body
