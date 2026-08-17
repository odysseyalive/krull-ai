"""
Intent routing (C3) — classify a web query on a SEPARATE, deterministic pass so
the weak local model never has to emit a tool call to steer web access.

Mirrors the Claude Code yoloClassifier pattern: intent is decided by a cheap
heuristic on the query text, out of band from the model's own generation. The
model never sees or drives this; it only receives the results.

Heuristics are ordered by specificity: an explicit "exhaustive" ask wins over a
media word, which wins over the plain-text default.
"""

import re

# Deterministic, no model. Word-boundary anchored so "imagine" does not read as
# "image" and "overview" does not read as "view".
_VIDEO = re.compile(r"\b(video|videos|youtube|watch|clip|clips|footage|trailer)\b", re.I)
_IMAGE = re.compile(r"\b(image|images|photo|photos|picture|pictures|pic|pics|logo|diagram|screenshot|wallpaper)\b", re.I)
_EXHAUSTIVE = re.compile(
    r"\b(exhaustive|thorough|thoroughly|deep dive|comprehensive|"
    r"everything about|all sources|in[- ]depth|research)\b",
    re.I,
)

# Intents the broker knows how to route. Kept as plain strings so callers and
# tests do not depend on an enum import.
TEXT = "text"
IMAGE = "image"
VIDEO = "video"
EXHAUSTIVE = "exhaustive"


def classify(query: str) -> str:
    """Return one of TEXT / IMAGE / VIDEO / EXHAUSTIVE for a raw query string."""
    if not query:
        return TEXT
    if _EXHAUSTIVE.search(query):
        return EXHAUSTIVE
    if _VIDEO.search(query):
        return VIDEO
    if _IMAGE.search(query):
        return IMAGE
    return TEXT


# Map an intent to the SearXNG category used for primary discovery. EXHAUSTIVE
# discovers in the general category and additionally escalates to direct
# engine fetches (handled by the caller), so it maps to general here.
_CATEGORY = {
    TEXT: "general",
    IMAGE: "images",
    VIDEO: "videos",
    EXHAUSTIVE: "general",
}


def searxng_category(intent: str) -> str:
    return _CATEGORY.get(intent, "general")
