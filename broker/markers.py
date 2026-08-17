"""
Honest empty/blocked markers (C5) — the direct fix for the "I can't browse"
hallucination.

Mirrors Claude Code's isToolResultContentEmpty + injected-marker pattern
(utils/toolResultStorage.ts): a tool that produced nothing must not hand back a
silent empty result, because a weak model reads silence as "browsing is
impossible" and fabricates an apology. Instead the broker always returns at
least one synthetic result the RAG pipeline will inject, naming the real reason
so the model reports it honestly.

Every reason is a distinct state — never absent, never a synthesized answer.
The search-result shape is the external-engine contract: {link, title, snippet}.
"""

# Distinct, machine-set reasons. The value is the human-readable clause spliced
# into the snippet; the key is what the broker's control flow selects.
REASON_NO_RESULTS = "returned no results"
REASON_NO_CONTENT = "loaded but had no readable content"
REASON_RATE_LIMITED = "was rate-limited by the search backend"
REASON_ENGINE_ERROR = "failed because the search engine could not be reached"
REASON_TIMED_OUT = "timed out before any results came back"
REASON_CAPTCHA_UNSOLVED = "hit a CAPTCHA / bot wall that was not solved in time"
REASON_BLOCKED = "was blocked by the target site"
REASON_LOGIN_WALL = "requires a login the stack does not have"
REASON_PAYWALL = "is behind a paywall"


def unavailable_result(query: str, reason: str) -> dict:
    """One search result, in the {link,title,snippet} external-engine shape,
    telling the model the search did not complete and why."""
    return {
        "link": "",
        "title": "Web search unavailable",
        "snippet": (
            f'The web search for "{query}" {reason}. '
            "No live results were retrieved. Say so honestly to the user; "
            "do not claim you are unable to browse the web, and do not invent "
            "an answer as if you had searched."
        ),
    }


def unavailable_search(query: str, reason: str) -> list:
    """Search endpoint failure payload: a single-element result list. Never []."""
    return [unavailable_result(query, reason)]


def unavailable_document(url: str, reason: str) -> dict:
    """One document, in the external-loader shape {page_content, metadata},
    telling the model the page could not be read and why."""
    return {
        "page_content": (
            f"[The page at {url} could not be retrieved: it {reason}.] "
            "No content was loaded. Report this honestly; do not fabricate the "
            "page's contents."
        ),
        "metadata": {"source": url, "krull_fetch_status": "unavailable", "reason": reason},
    }
