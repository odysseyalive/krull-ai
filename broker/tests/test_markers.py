"""Unit tests for honest empty/blocked markers (C5)."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import markers  # noqa: E402


def test_search_marker_shape_is_external_engine_contract():
    out = markers.unavailable_search("hawaii volcanoes", markers.REASON_RATE_LIMITED)
    # Never an empty list — always exactly one injected result.
    assert isinstance(out, list) and len(out) == 1
    r = out[0]
    assert set(r.keys()) == {"link", "title", "snippet"}
    assert r["link"] == ""
    assert "hawaii volcanoes" in r["snippet"]
    assert "rate-limited" in r["snippet"]


def test_search_marker_forbids_the_hallucination():
    r = markers.unavailable_search("q", markers.REASON_CAPTCHA_UNSOLVED)[0]
    # The direct fix for the Hawaii chat: the model is told NOT to claim it cannot browse.
    assert "do not claim you are unable to browse" in r["snippet"].lower()
    assert "do not invent" in r["snippet"].lower()


def test_distinct_reasons_render_distinctly():
    reasons = [
        markers.REASON_NO_RESULTS,
        markers.REASON_NO_CONTENT,
        markers.REASON_RATE_LIMITED,
        markers.REASON_ENGINE_ERROR,
        markers.REASON_TIMED_OUT,
        markers.REASON_CAPTCHA_UNSOLVED,
        markers.REASON_BLOCKED,
        markers.REASON_LOGIN_WALL,
        markers.REASON_PAYWALL,
    ]
    snippets = {markers.unavailable_search("q", r)[0]["snippet"] for r in reasons}
    # Every reason produces a distinct snippet — no collapsing into one generic string.
    assert len(snippets) == len(reasons)


def test_document_marker_shape_is_external_loader_contract():
    d = markers.unavailable_document("https://example.com/x", markers.REASON_BLOCKED)
    assert set(d.keys()) == {"page_content", "metadata"}
    assert d["metadata"]["source"] == "https://example.com/x"
    assert "https://example.com/x" in d["page_content"]
    assert "do not fabricate" in d["page_content"].lower()
