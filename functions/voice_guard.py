"""
Open WebUI Filter: Voice Guard
Shapes assistant output toward a natural, conversational voice by stripping
common "AI tells" — em-dashes, stock preambles/closers, contrastive hedges
("it's not just X, it's Y"), and overused vocabulary.

Two passes:
  - inlet: injects a system-level voice directive so the model tries to
    avoid tells in the first place.
  - outlet: post-processes the completed response with conservative regex
    substitutions as a safety net for the worst offenders.

Outlet is intentionally narrow. Broad vocabulary replacement (delve,
tapestry, landscape, etc.) is handled by the inlet prompt only, because
those words have legitimate uses and auto-replacement mangles technical
writing. Only replace patterns that are almost always an AI tell.
"""

import re
from pydantic import BaseModel, Field
from typing import Optional


VOICE_GUARD_CONTENT = (
    "[Voice Guard — Natural Conversational Voice]\n\n"
    "Write like a competent human talking to another competent human. "
    "Be direct, warm, and plain.\n\n"
    "AVOID these AI tells:\n"
    "  - Em-dashes (—). Use commas, periods, or parentheses instead.\n"
    "  - Stock openers: 'Certainly!', 'Absolutely!', 'Great question!', "
    "'I'd be happy to', 'Of course!'.\n"
    "  - Stock closers: 'Let me know if you have any questions', "
    "'Feel free to ask', 'I hope this helps'.\n"
    "  - Contrastive hedges: 'It's not just X, it's Y', "
    "'not merely X but Y', 'more than just X'.\n"
    "  - Overused vocab: delve, tapestry, landscape, realm, journey, "
    "navigate (metaphorical), leverage (verb), robust, seamless, "
    "holistic, synergy, unlock, unleash, elevate, empower.\n"
    "  - Tricolon rhythm and faux-parallel lists when a plain sentence works.\n"
    "  - Needless hedging: 'it's worth noting that', 'it's important to "
    "remember', 'keep in mind'.\n\n"
    "Prefer: contractions, short sentences, concrete nouns, plain verbs. "
    "Answer the question first, explain after. If something is obvious, "
    "don't say it.\n\n"
    "CITATIONS — MANDATORY FORMAT:\n"
    "When you use information from web search results, Kiwix articles, or "
    "any injected source context, you MUST cite them as clickable markdown "
    "links: [Source Title](https://url). Never write a citation as plain "
    "text (e.g. 'The Guardian (2026/04/14). Title.') — that loses the URL "
    "and the user cannot click through.\n"
    "  - Inline form: '...according to [The Guardian](https://...).'\n"
    "  - Reference list form: '- [Title](https://url) — one-line note'\n"
    "  - The URL comes from the 'URL:' line in the injected search results. "
    "Copy it verbatim into the markdown link. If no URL is available, "
    "don't fabricate one — cite by name only and say the URL is unavailable.\n"
    "  - Never omit the URL because it's 'long' or 'ugly'. The user wants "
    "the link.\n"
    "[End Voice Guard]"
)


# Outlet substitutions. Order matters: longer phrases first so they
# don't get partially eaten by shorter patterns.
_OUTLET_SUBS: list[tuple[re.Pattern, str]] = [
    # Stock openers at start of response. Strip the greeting word plus
    # its immediate punctuation; leave the following sentence intact.
    # (Earlier versions tried to strip the whole sentence with
    # [^.\n]*[.!] but that pattern treated periods inside URLs as
    # sentence ends and devoured markdown links.)
    (re.compile(
        r"(?:^|\n\n)(?:Certainly|Absolutely|Of course|Sure thing|Great question"
        r"|Excellent question|I'd be happy to help|I'd be glad to help"
        r"|Happy to help)[!.,]\s+",
        re.I,
    ), ""),

    # Stock closers — a full final sentence. The terminating [.!?] must
    # be followed by whitespace or end-of-string (lookahead), so periods
    # inside URLs don't count as sentence ends.
    (re.compile(
        r"\s*(?:Let me know if (?:you have|there's|you need)[^\n]*?[.!?](?=\s|$)"
        r"|Feel free to (?:ask|reach out|let me know)[^\n]*?[.!?](?=\s|$)"
        r"|I hope (?:this|that) helps[^\n]*?[.!?](?=\s|$)"
        r"|Hope (?:this|that) helps[^\n]*?[.!?](?=\s|$))\s*$",
        re.I,
    ), ""),

    # Needless hedging stems.
    (re.compile(r"\bIt'?s worth noting that\s+", re.I), ""),
    (re.compile(r"\bIt'?s important to (?:note|remember|mention) that\s+", re.I), ""),
    (re.compile(r"\bKeep in mind that\s+", re.I), ""),

    # Em-dashes. Space-padded version becomes a comma; bare becomes hyphen.
    # Handle both ASCII " -- " and unicode " — ".
    (re.compile(r"\s+[—–]\s+"), ", "),
    (re.compile(r"\s+--\s+"), ", "),
    (re.compile(r"[—–]"), "-"),
]


class Filter:
    class Valves(BaseModel):
        priority: int = Field(
            default=5,
            description="Filter priority (runs after content-injecting filters)",
        )
        enabled: bool = Field(
            default=True, description="Enable/disable voice guard"
        )
        strip_em_dashes: bool = Field(
            default=True,
            description="Post-process em-dashes into commas/hyphens",
        )
        strip_stock_phrases: bool = Field(
            default=True,
            description="Post-process stock openers/closers/hedges",
        )

    def __init__(self):
        self.valves = self.Valves()

    async def inlet(self, body: dict, __user__: Optional[dict] = None) -> dict:
        if not self.valves.enabled:
            return body
        messages = body.get("messages", [])
        messages.insert(0, {"role": "system", "content": VOICE_GUARD_CONTENT})
        body["messages"] = messages
        return body

    def _clean(self, text: str) -> str:
        for pat, repl in _OUTLET_SUBS:
            # Skip dash subs if disabled
            if not self.valves.strip_em_dashes and (
                "—" in pat.pattern or "--" in pat.pattern or "–" in pat.pattern
            ):
                continue
            # Skip phrase subs if disabled
            if not self.valves.strip_stock_phrases and (
                "Certainly" in pat.pattern
                or "worth noting" in pat.pattern
                or "Let me know" in pat.pattern
                or "Keep in mind" in pat.pattern
                or "important to" in pat.pattern
            ):
                continue
            text = pat.sub(repl, text)
        return text

    async def outlet(self, body: dict, __user__: Optional[dict] = None) -> dict:
        if not self.valves.enabled:
            return body
        messages = body.get("messages", [])
        for msg in messages:
            if msg.get("role") != "assistant":
                continue
            content = msg.get("content")
            if isinstance(content, str):
                msg["content"] = self._clean(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        part["text"] = self._clean(part["text"])
        return body
