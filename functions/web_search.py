"""
Open WebUI Inlet Filter: intent-gated web access via the krull-web-broker.

This filter is the TRIGGER; the broker is the mechanism. When a user turn
genuinely calls for live/external information, the filter POSTs {query, count} to
the broker's /search endpoint (the OWUI external-engine contract) and injects the
returned [{link, title, snippet}] results into context before the model responds.

The broker owns intent ROUTING (text/image/video/exhaustive → SearXNG categories
+ host playwright-mcp page reads + headed CAPTCHA handoff) and honest failure
markers, so this filter stays thin: gate, forward, inject.

Why gate at all: firing on every turn (conversational follow-ups, "explain
this", code questions) forces the small thinking-mode model to reason over
injected sources it never needed — the multi-minute-latency / citation-bloat
failure recorded in the awareness ledger (PAT-2026-04-15). The gate is
load-bearing; keep it.
"""

import re
from datetime import datetime
from pydantic import BaseModel, Field
from typing import Optional


# Web search fires only when the query genuinely calls for live or external
# information — not on conversational follow-ups or self-contained questions.
#
# The media + video patterns MUST mirror the broker's own intent router
# (broker/router.py _IMAGE / _VIDEO): the broker has dedicated image/video
# routing, but it is only reachable if this gate fires first. Without them, a
# request like "show me pictures of the storm this morning" never triggers a
# search, so the model gets no results AND no honest marker and confabulates a
# refusal (the "Hurricane Lala" failure). A request for a picture or video of a
# real-world event cannot be answered honestly from training weights, so these
# are unambiguous web intent, not over-firing. Keep the two lists in sync.
_WEB_TRIGGERS = [
    # recency (incl. "this morning / tonight / yesterday" for recent events)
    re.compile(r"\b(?:latest|recent|current|today|tonight|yesterday|now|news|update|breaking)\b", re.I),
    re.compile(r"\bthis (?:morning|afternoon|evening|week|month|year)\b", re.I),
    # explicit search intent
    re.compile(r"\b(?:search|google|look up|find online|on the web|pull up)\b", re.I),
    # image intent — mirrors broker/router.py _IMAGE
    re.compile(r"\b(?:image|images|photo|photos|picture|pictures|pic|pics|logo|diagram|screenshot|wallpaper)\b", re.I),
    # video intent — mirrors broker/router.py _VIDEO
    re.compile(r"\b(?:video|videos|youtube|watch|clip|clips|footage|trailer)\b", re.I),
    # factual questions
    re.compile(r"\b(?:who is|what is|when did|where did|how many|how much)\b", re.I),
    re.compile(r"\b(?:price|stock|score|weather|release date|version)\b", re.I),
    re.compile(r"\?\s*$"),
]


def _wants_web_search(text: str) -> bool:
    if not text:
        return False
    return any(p.search(text) for p in _WEB_TRIGGERS)


def _extract_text(content) -> str:
    """A user message's content is usually a str, but multimodal turns carry a
    list of parts ({type:"text"|"image_url", ...}). Pull the text out so the
    filter never calls .strip() on a list (which would raise and error the whole
    inlet, dropping the user's turn)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            p.get("text", "")
            for p in content
            if isinstance(p, dict) and p.get("type") == "text"
        )
    return ""


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
        broker_url: str = Field(
            default="http://krull-web-broker:8130/search",
            description="krull-web-broker /search endpoint (external-engine contract)",
        )
        num_results: int = Field(
            default=5, description="Number of search results to request from the broker"
        )
        timeout: int = Field(
            default=90,
            description=(
                "Request timeout (s). The broker's /search never opens headed "
                "CAPTCHA windows (exhaustive deepening fetches without solving), "
                "so it is bounded by SearXNG + a few page reads — 90s is ample. "
                "If it still fires, the broker is genuinely wedged and the honest "
                "'unreachable' marker is correct."
            ),
        )
        enabled: bool = Field(
            default=True, description="Enable/disable web search injection"
        )

    def __init__(self):
        self.valves = self.Valves()

    def _render(self, results: list) -> str:
        """Turn broker [{link,title,snippet}] into the injected context block.
        Soft citation language only — stacking hard 'MUST cite' demands across
        filters compounds into multi-minute responses on the thinking model
        (PAT-2026-04-15)."""
        date_str = datetime.now().strftime("%B %d, %Y")
        lines = [f"[Web Search Results — retrieved {date_str}]"]
        for i, r in enumerate(results, 1):
            title = r.get("title", "")
            link = r.get("link", "")
            snippet = r.get("snippet", "")
            loc = f"\n   URL: {link}" if link else ""
            lines.append(f"{i}. {title}{loc}\n   {snippet}")
        lines.append("[End Web Search Results]")
        lines.append("")
        lines.append(
            f"Live web results retrieved on {date_str}; treat as current "
            "and supersede training data. Cite any you actually use."
        )
        lines.append("")
        return "\n".join(lines)

    async def inlet(self, body: dict, __user__: Optional[dict] = None) -> dict:
        if not self.valves.enabled:
            return body

        messages = body.get("messages", [])
        if not messages:
            return body

        last_message = messages[-1]
        if last_message.get("role") != "user":
            return body

        # Use the user's ORIGINAL query, not whatever previous inlet filters may
        # have prepended. The first filter to run stashes the clean query; later
        # filters read it back.
        query = body.get("_krull_original_query")
        if query is None:
            query = _extract_text(last_message.get("content", ""))
            body["_krull_original_query"] = query
        if not query or len(query.strip()) < 3:
            return body

        if not _wants_web_search(query):
            return body

        results = None
        try:
            import aiohttp

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self.valves.broker_url,
                    json={"query": query, "count": self.valves.num_results},
                    timeout=aiohttp.ClientTimeout(total=self.valves.timeout),
                ) as resp:
                    if resp.status == 200:
                        results = await resp.json()
        except Exception:
            results = None

        # C5 end-to-end: the broker guarantees ≥1 honest result, but if the
        # broker itself is unreachable we must still not fall silent — a silent
        # empty read as "I can't browse" to the model. Inject our own honest
        # marker naming the reason instead.
        if not isinstance(results, list) or not results:
            results = [
                {
                    "link": "",
                    "title": "Web search unavailable",
                    "snippet": (
                        f'The web search for "{query}" could not complete: the '
                        "web-access broker was unreachable. No live results were "
                        "retrieved. Say so honestly; do not claim you are unable "
                        "to browse the web, and do not invent an answer."
                    ),
                }
            ]

        search_context = self._render(results)

        # Prepend rather than overwrite so kiwix_lookup / map_search inlet filters
        # compose cleanly — whichever runs later keeps the others' contributions.
        # Handle both plain-string and multimodal (list-of-parts) content shapes.
        content = messages[-1].get("content", "")
        if isinstance(content, list):
            messages[-1]["content"] = [{"type": "text", "text": search_context}] + content
        else:
            messages[-1]["content"] = f"{search_context}\n{content}"
        return body
