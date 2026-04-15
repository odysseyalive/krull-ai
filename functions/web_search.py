"""
Open WebUI Inlet Filter: Auto Web Search via SearXNG
Automatically searches SearXNG for every user query and injects
the top results into the context before the model responds.
"""

import re
import urllib.parse
import json
from datetime import datetime
from pydantic import BaseModel, Field
from typing import Optional


# Web search should fire only when the query genuinely calls for live or
# external information. Firing on every turn (including conversational
# follow-ups, code questions, "explain this") forces the model to reason
# over 5 injected sources it never needed, which on small thinking-mode
# models compounds into multi-minute response times.
_WEB_TRIGGERS = [
    re.compile(r"\b(?:latest|recent|current|today|now|news|update|breaking)\b", re.I),
    re.compile(r"\b(?:search|google|look up|find online|on the web)\b", re.I),
    re.compile(r"\b(?:who is|what is|when did|where did|how many|how much)\b", re.I),
    re.compile(r"\b(?:price|stock|score|weather|release date|version)\b", re.I),
    re.compile(r"\?\s*$"),
]


def _wants_web_search(text: str) -> bool:
    if not text:
        return False
    return any(p.search(text) for p in _WEB_TRIGGERS)


class Filter:
    class Valves(BaseModel):
        priority: int = Field(
            default=2,
            description=(
                "Filter priority (lower runs first). Web search runs "
                "AFTER kiwix_lookup so the user's curated offline "
                "library gets the closer-to-question position."
            ),
        )
        searxng_url: str = Field(
            default="http://krull-searxng:8080",
            description="SearXNG instance URL",
        )
        num_results: int = Field(
            default=5, description="Number of search results to include"
        )
        enabled: bool = Field(
            default=True, description="Enable/disable web search injection"
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
        # filters may have prepended. The first filter to run stashes
        # the clean query on the body; subsequent filters read it back.
        query = body.get("_krull_original_query")
        if query is None:
            query = last_message.get("content", "")
            body["_krull_original_query"] = query
        if not query or len(query.strip()) < 3:
            return body

        if not _wants_web_search(query):
            return body

        try:
            import aiohttp

            # Add date context to searches about recent/latest/current events
            search_query = query
            recency_words = ["latest", "recent", "current", "today", "new", "now", "update"]
            if any(word in query.lower() for word in recency_words):
                now = datetime.now()
                date_suffix = now.strftime("%B %Y")
                search_query = f"{query} {date_suffix}"

            search_url = (
                f"{self.valves.searxng_url}/search"
                f"?q={urllib.parse.quote(search_query)}"
                f"&format=json"
                f"&categories=general"
            )

            async with aiohttp.ClientSession() as session:
                async with session.get(search_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        return body
                    data = await resp.json()

            results = data.get("results", [])[: self.valves.num_results]
            if not results:
                return body

            now = datetime.now()
            date_str = now.strftime("%B %d, %Y")

            context_lines = [f"[Web Search Results — retrieved {date_str}]"]
            for i, r in enumerate(results, 1):
                title = r.get("title", "")
                url = r.get("url", "")
                snippet = r.get("content", "")
                context_lines.append(f"{i}. {title}\n   URL: {url}\n   {snippet}")
            context_lines.append("[End Web Search Results]")
            context_lines.append("")
            context_lines.append(
                f"Live web results retrieved on {date_str}; treat as current "
                "and supersede training data. Cite any you actually use."
            )
            context_lines.append("")

            search_context = "\n".join(context_lines)

            # Prepend web results to the existing message content rather
            # than overwrite it. This lets multiple inlet filters
            # (kiwix_lookup, map_search) compose cleanly — whichever
            # runs later sees the others' contributions and adds its own
            # on top, instead of wiping them out.
            messages[-1]["content"] = (
                f"{search_context}\n{messages[-1]['content']}"
            )

        except Exception:
            pass

        return body
