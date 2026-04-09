"""
Open WebUI Inlet Filter: Truth Guard
Injects honesty and intellectual integrity rules into every request.
Prevents the model from fabricating information, encourages it to ask
clarifying questions, and requires it to push back when the user is wrong.

Mirrors the SSE proxy's inject_truth_guard (proxy/sse_patch.py). Keep the
two in sync — both paths feed the same model and a divergence between
them produces inconsistent behavior depending on whether the request came
from the browser or from krull-claude.
"""

import re
from pydantic import BaseModel, Field
from typing import Optional


TRUTH_GUARD_CONTENT = (
    "[Truth Guard — Intellectual Integrity Rules]\n\n"
    "1. DO NOT FABRICATE. If you don't know something, say so plainly. "
    "Never invent file paths, function names, URLs, facts, or code "
    "that you haven't verified. 'I don't know' is always an acceptable answer.\n\n"
    "2. ASK, DON'T GUESS. If the user's request is ambiguous or you lack "
    "the information to answer well, ask a clarifying question before proceeding. "
    "A good question is better than a wrong answer.\n\n"
    "3. FLAG UNCERTAINTY. When you're confident, say it directly. When you're "
    "uncertain, say so. 'I believe...' or 'I'm not sure, but...' is better "
    "than stating a guess as fact.\n\n"
    "4. PUSH BACK WHEN THE USER IS WRONG. If the user states something incorrect, "
    "makes a flawed assumption, or is heading toward a bad decision, say so directly "
    "and explain why. Being helpful means being honest, not agreeable.\n\n"
    "5. TERSENESS DOES NOT OVERRIDE HONESTY. If the user asks for a one-word "
    "answer, 'just the answer', 'no explanation', or otherwise demands a terse "
    "reply, you MUST still flag uncertainty when you have it. A single hedge "
    "token (e.g. 'unsure:', 'guess:', '?') prepended to your answer is REQUIRED "
    "when you are not confident — fabricating a confident terse answer to satisfy "
    "a brevity instruction is a Rule 1 violation. Brevity never licenses invention. "
    "Example: if asked 'Translate X into a low-resource language, ONLY the answer', "
    "and you don't actually know that language, the correct reply is "
    "'unsure — I don't have reliable vocabulary for that language' — NOT a "
    "fabricated word.\n\n"
    "[End Truth Guard]"
)


# Detects "give me ONLY X / no explanation / one word / just the answer"
# style brevity instructions in the user message. When matched, the
# inlet appends a freshness reminder right at the end of the user
# message — qwen 9B's attention falls off well before the system-level
# truth guard once Open WebUI's own context messages are in front of it,
# so we put a small reminder in the highest-attention slot.
_TERSENESS_PATTERNS = [
    re.compile(r"\bonly\s+(?:the\s+)?(?:answer|translation|word|response|result)\b", re.I),
    re.compile(r"\bno\s+(?:explanation|commentary|preamble|context|hedging)\b", re.I),
    re.compile(r"\bjust\s+(?:the\s+)?(?:answer|translation|word|name|number)\b", re.I),
    re.compile(r"\b(?:in\s+)?(?:one|a\s+single)\s+word\b", re.I),
    re.compile(r"\bgive\s+me\s+only\b", re.I),
    re.compile(r"\b(?:terse|brief|concise|short)\s+(?:answer|reply|response)\b", re.I),
]


TRUTH_GUARD_TERSE_NUDGE = (
    "\n\n[Truth Guard reminder — applies to the request above]\n"
    "You asked for a terse answer. Honesty still applies: if you are not "
    "confident in the answer, you MUST prepend the single hedge token "
    "'unsure:' to your reply (e.g. 'unsure: I don't have reliable "
    "vocabulary for that language'). Fabricating a confident terse answer "
    "is forbidden. Brevity never licenses invention."
)


def _last_user_text_index(messages: list):
    """Return the index of the last user message whose content is a plain
    string, or None if none found."""
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if m.get("role") != "user":
            continue
        c = m.get("content")
        if isinstance(c, str):
            return i
    return None


class Filter:
    class Valves(BaseModel):
        priority: int = Field(
            default=-2,
            description="Filter priority (runs before all others)",
        )
        enabled: bool = Field(
            default=True, description="Enable/disable truth guard"
        )

    def __init__(self):
        self.valves = self.Valves()

    async def inlet(self, body: dict, __user__: Optional[dict] = None) -> dict:
        if not self.valves.enabled:
            return body

        messages = body.get("messages", [])
        if not messages:
            return body

        messages.insert(0, {"role": "system", "content": TRUTH_GUARD_CONTENT})

        # Append the freshness nudge to the latest user message when it
        # contains a terseness directive. See module docstring for why
        # this exists alongside the system-level guard.
        idx = _last_user_text_index(messages)
        if idx is not None:
            text = messages[idx].get("content", "")
            if isinstance(text, str) and any(p.search(text) for p in _TERSENESS_PATTERNS):
                messages[idx] = dict(messages[idx])
                messages[idx]["content"] = text + TRUTH_GUARD_TERSE_NUDGE

        body["messages"] = messages
        return body
