"""
SearXNG discovery client — the reliable, in-stack primary for text/image/video
discovery. Reuses the JSON API the retired-in-place functions/web_search.py
filter used, extended with intent→category routing.

Returns the external-engine {link, title, snippet} shape directly, so both the
broker's /search endpoint and (if ever flipped to the native external engine)
OWUI consume the same objects. Raises on transport failure so the caller can
attach the correct honest marker; returns [] only for a genuine zero-hit.
"""

import asyncio
import re
from datetime import datetime
from urllib.parse import quote

import aiohttp

# Recency terms that benefit from a "Month Year" suffix so time-sensitive
# queries bias toward current results. Word-boundary anchored so "now" does not
# match "knowledge" / "nowhere" and "new" does not match "renew" (the old
# filter's substring test over-fired on exactly these).
_RECENCY = re.compile(r"\b(latest|recent|current|today|new|now|update)\b", re.I)


class SearxngError(RuntimeError):
    """SearXNG could not be reached or returned a non-200 status."""


def _snippet_for(intent: str, r: dict) -> str:
    """Build a snippet enriched with the media affordances the model needs to
    reason about an image or video hit, not just the text blurb."""
    base = (r.get("content") or "").strip()
    if intent == "image":
        img = r.get("img_src") or r.get("thumbnail_src")
        return f"{base} [image: {img}]" if img else base
    if intent == "video":
        length = r.get("length")
        tail = f" [duration: {length}]" if length else ""
        return f"{base}{tail}"
    return base


async def search(
    searxng_url: str,
    query: str,
    count: int,
    category: str = "general",
    intent: str = "text",
    timeout: float = 12.0,
) -> list:
    """Query SearXNG and return up to `count` {link,title,snippet} results.

    Raises SearxngError on transport/status failure (distinct from an empty hit
    list) so the caller can pick a rate-limited / engine-error / no-results
    marker deliberately.
    """
    search_query = query
    if _RECENCY.search(query):
        search_query = f"{query} {datetime.now().strftime('%B %Y')}"

    url = (
        f"{searxng_url}/search"
        f"?q={quote(search_query)}"
        f"&format=json"
        f"&categories={quote(category)}"
    )

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=timeout)
            ) as resp:
                if resp.status == 429:
                    raise SearxngError("rate-limited")
                if resp.status != 200:
                    raise SearxngError(f"status {resp.status}")
                data = await resp.json()
    except asyncio.TimeoutError as e:
        raise SearxngError("timeout") from e
    except aiohttp.ClientError as e:
        raise SearxngError(str(e)) from e

    results = data.get("results", [])[:count]
    out = []
    for r in results:
        link = r.get("url") or ""
        title = r.get("title") or link or "(untitled)"
        out.append(
            {"link": link, "title": title, "snippet": _snippet_for(intent, r)}
        )
    return out
