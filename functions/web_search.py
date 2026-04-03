"""
Open WebUI Inlet Filter: Auto Web Search via SearXNG
Automatically searches SearXNG for every user query and injects
the top results into the context before the model responds.
"""

import urllib.parse
import json
from pydantic import BaseModel, Field
from typing import Optional


class Filter:
    class Valves(BaseModel):
        priority: int = Field(
            default=1, description="Filter priority (lower runs first)"
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

        query = last_message.get("content", "")
        if not query or len(query.strip()) < 3:
            return body

        try:
            import aiohttp

            search_url = (
                f"{self.valves.searxng_url}/search"
                f"?q={urllib.parse.quote(query)}"
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

            context_lines = ["[Web Search Results]"]
            for i, r in enumerate(results, 1):
                title = r.get("title", "")
                url = r.get("url", "")
                snippet = r.get("content", "")
                context_lines.append(f"{i}. {title}\n   {url}\n   {snippet}")
            context_lines.append("[End Web Search Results]\n")

            search_context = "\n".join(context_lines)

            messages[-1]["content"] = (
                f"{search_context}\n\nUser question: {query}"
            )

        except Exception:
            pass

        return body
