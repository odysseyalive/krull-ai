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


# Common English stop words + conversational fillers. We strip these
# from the user's query before passing it to kiwix's full-text search,
# because kiwix is keyword-based and a sentence like "can you hunt up
# a nice dutch oven recipe for stew meat and potatoes" otherwise gets
# diluted by all the function words and surfaces tangential matches
# instead of directly-relevant Q&A and reference content.
_STOP_WORDS = frozenset(
    """
    a an and any are as at be been being but by can could did do does done
    for from get give go got had has have having he her here him his how i
    if in into is it its just like make me my no not now of off on once one
    only or our out over own please same shall she should show so some such
    tell than that the their them then there these they this those through
    to too under until up upon us very was way we were what when where
    which who whom whose why will with would you your yours
    nice good great best bad simple easy quick fast slow
    find search hunt look looking lookup recommend recommendation suggest
    suggestion want need help hint tip
    """.split()
)


def _extract_keywords(text: str) -> str:
    """Strip stop words and conversational fillers from a user query so
    kiwix's keyword full-text search gets a clean signal. Falls back to
    the original text if stripping leaves nothing."""
    tokens = re.findall(r"[A-Za-z][A-Za-z'-]*", text.lower())
    keywords = [t for t in tokens if t not in _STOP_WORDS and len(t) > 1]
    return " ".join(keywords) if keywords else text


# URL patterns that mark Stack Exchange tag/index pages and other
# navigational content. These pages contain the search terms (because
# they list every question with that tag) but they're useless to a
# language model — they're just lists of links. We skip them in
# post-processing so the model only sees actual content pages.
_JUNK_URL_PATTERNS = [
    re.compile(r"/questions/tagged/"),     # Stack Exchange tag listings
    re.compile(r"/questions\?"),           # Stack Exchange query pages
    re.compile(r"/users/"),                # User profile pages
    re.compile(r"/tags/"),                 # Tag index pages
    re.compile(r"_page%3D"),               # Pagination URL-encoded
    re.compile(r"_page=\d"),               # Pagination plain
]


def _is_junk_link(link: str) -> bool:
    """Return True for kiwix result links that aren't actual content."""
    if not link:
        return False
    return any(p.search(link) for p in _JUNK_URL_PATTERNS)


class Filter:
    class Valves(BaseModel):
        priority: int = Field(
            default=1,
            description=(
                "Filter priority (lower runs first). Kiwix runs BEFORE "
                "web_search so its context ends up closest to the user "
                "question in the final message — model attention bias "
                "puts more weight on instructions near the question."
            ),
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

        # Use the user's ORIGINAL query, not whatever previous inlet
        # filters (web_search, map_search, etc) may have prepended into
        # the message. The first filter to run stashes the clean query
        # on the body; subsequent filters read it back. Without this,
        # whichever filter runs second sees a polluted multi-paragraph
        # blob as its "search query" and returns nothing useful.
        query = body.get("_krull_original_query")
        if query is None:
            query = last_message.get("content", "")
            body["_krull_original_query"] = query
        if not query or len(query.strip()) < 3:
            return body

        # Strip stop words and conversational fillers — kiwix is a
        # keyword full-text search engine, not semantic, so a long
        # natural-language question dilutes the relevance signal.
        search_pattern = _extract_keywords(query)

        try:
            import aiohttp

            # Over-fetch so we have headroom to filter out junk results
            # (tag listings, index pages, etc) and still end up with
            # num_results actual content items.
            search_url = (
                f"{self.valves.kiwix_url}/search"
                f"?pattern={urllib.parse.quote(search_pattern)}"
                f"&format=xml"
                f"&pageLength={self.valves.num_results * 4}"
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

            raw_items = channel.findall("item")
            if not raw_items:
                return body

            # Filter out junk: tag listings, index pages, etc. Keep up
            # to num_results actual content items.
            items = []
            for item in raw_items:
                link_el = item.find("link")
                link = link_el.text if link_el is not None else ""
                if _is_junk_link(link):
                    continue
                items.append(item)
                if len(items) >= self.valves.num_results:
                    break

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
                "IMPORTANT: The results above come from the user's curated "
                "offline knowledge library — books from Project Gutenberg, "
                "Stack Exchange Q&A, dev docs, Wikipedia, and other reference "
                "archives stored locally. These are FIRST-CLASS sources. You "
                "MUST use them when they're relevant to the question, and you "
                "MUST cite them inline (e.g., \"according to [Article Title]"
                "(URL)...\") alongside any web search results that may also "
                "appear below. If both offline knowledge and web search "
                "return relevant results, cite BOTH in your References "
                "section at the end of your response — do not silently drop "
                "the offline sources in favour of the web ones."
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
