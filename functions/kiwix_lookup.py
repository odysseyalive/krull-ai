"""
Open WebUI Inlet Filter: Kiwix Offline Knowledge Lookup
Searches the local Kiwix instance for relevant articles and injects
summaries into the context before the model responds.
"""

import urllib.parse
from pydantic import BaseModel, Field
from typing import Optional


class Filter:
    class Valves(BaseModel):
        priority: int = Field(
            default=2, description="Filter priority (lower runs first)"
        )
        kiwix_url: str = Field(
            default="http://krull-kiwix:8080",
            description="Kiwix instance URL",
        )
        num_results: int = Field(
            default=3, description="Number of Kiwix results to include"
        )
        max_snippet_length: int = Field(
            default=500, description="Max characters per article snippet"
        )
        enabled: bool = Field(
            default=True, description="Enable/disable Kiwix lookup"
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
                f"{self.valves.kiwix_url}/search"
                f"?pattern={urllib.parse.quote(query)}"
                f"&pageLength={self.valves.num_results}"
            )

            async with aiohttp.ClientSession() as session:
                async with session.get(search_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        return body
                    text = await resp.text()

            # Kiwix returns HTML search results — extract what we can
            # Try the JSON search API first
            suggest_url = (
                f"{self.valves.kiwix_url}/suggest"
                f"?term={urllib.parse.quote(query)}"
                f"&limit={self.valves.num_results}"
            )

            async with aiohttp.ClientSession() as session:
                async with session.get(suggest_url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status != 200:
                        return body
                    suggestions = await resp.json()

            if not suggestions:
                return body

            context_lines = ["[Offline Knowledge Base (Kiwix)]"]
            for i, item in enumerate(suggestions, 1):
                title = item.get("label", item.get("value", ""))
                path = item.get("path", item.get("url", ""))
                if title:
                    snippet = ""
                    if path:
                        # Fetch article snippet
                        try:
                            article_url = f"{self.valves.kiwix_url}{path}"
                            async with aiohttp.ClientSession() as session:
                                async with session.get(article_url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                                    if resp.status == 200:
                                        html = await resp.text()
                                        # Strip HTML tags for a rough snippet
                                        import re
                                        text = re.sub(r"<[^>]+>", " ", html)
                                        text = re.sub(r"\s+", " ", text).strip()
                                        snippet = text[: self.valves.max_snippet_length]
                        except Exception:
                            pass

                    context_lines.append(f"{i}. {title}")
                    if snippet:
                        context_lines.append(f"   {snippet}")

            context_lines.append("[End Offline Knowledge Base]\n")

            if len(context_lines) > 2:
                knowledge_context = "\n".join(context_lines)
                messages[-1]["content"] = (
                    f"{knowledge_context}\n\n{messages[-1]['content']}"
                )

        except Exception:
            pass

        return body
