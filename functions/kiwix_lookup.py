"""
Open WebUI Inlet Filter: Kiwix Offline Knowledge Lookup
Searches the local Kiwix instance for relevant articles with full-text
snippets and injects them into the context before the model responds.
"""

import re
import urllib.parse
import xml.etree.ElementTree as ET
from pydantic import BaseModel, Field
from typing import Optional


def _xml_element_text(el) -> str:
    """Extract all text from an XML element, including text within child tags."""
    raw = ET.tostring(el, encoding="unicode", method="text")
    return raw.strip() if raw else ""


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
            default=800, description="Max characters per article snippet"
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

            # Use full-text search API (XML format) for content snippets
            search_url = (
                f"{self.valves.kiwix_url}/search"
                f"?pattern={urllib.parse.quote(query)}"
                f"&format=xml"
                f"&pageLength={self.valves.num_results}"
            )

            async with aiohttp.ClientSession() as session:
                async with session.get(
                    search_url, timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status != 200:
                        return body
                    xml_text = await resp.text()

            root = ET.fromstring(xml_text)
            channel = root.find("channel")
            if channel is None:
                return body

            items = channel.findall("item")
            if not items:
                return body

            context_lines = [
                "[Offline Knowledge Base (Kiwix) — full-text search results]"
            ]
            kiwix_public_url = "http://localhost:8090"

            for i, item in enumerate(items, 1):
                title_el = item.find("title")
                desc_el = item.find("description")
                link_el = item.find("link")
                book_el = item.find("book/title")

                title = title_el.text if title_el is not None else "Unknown"
                snippet = (
                    _xml_element_text(desc_el)
                    if desc_el is not None
                    else ""
                )
                link = link_el.text if link_el is not None else ""
                book = book_el.text if book_el is not None else ""

                context_lines.append(f"--- Result {i}: {title} ---")
                if book:
                    context_lines.append(f"Source: {book}")
                if snippet:
                    if len(snippet) > self.valves.max_snippet_length:
                        snippet = snippet[: self.valves.max_snippet_length] + "..."
                    context_lines.append(snippet)
                if link:
                    context_lines.append(f"Read more: {kiwix_public_url}{link}")
                context_lines.append("")

            context_lines.append("[End Offline Knowledge Base]")
            context_lines.append("")
            context_lines.append(
                "IMPORTANT: When using information from the offline knowledge "
                "base above, you MUST cite your sources. Reference them inline "
                '(e.g., "according to [Article Title](URL)...") and include '
                "a References section at the end of your response with the "
                "titles and URLs of all sources you used."
            )
            context_lines.append("")

            if len(context_lines) > 4:
                knowledge_context = "\n".join(context_lines)
                messages[-1]["content"] = (
                    f"{knowledge_context}\n{messages[-1]['content']}"
                )

        except Exception:
            pass

        return body
