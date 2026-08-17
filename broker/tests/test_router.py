"""Unit tests for intent routing (C3) — deterministic, no network, no model."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import router  # noqa: E402


def test_plain_factual_is_text():
    assert router.classify("what is the capital of Iceland") == router.TEXT
    assert router.classify("how does photosynthesis work") == router.TEXT


def test_image_words():
    for q in ["show me an image of a puffin", "photos of Reykjavik", "the NASA logo", "a diagram of the water cycle"]:
        assert router.classify(q) == router.IMAGE, q


def test_video_words():
    for q in ["video of a volcano erupting", "youtube clip of the aurora", "watch the SpaceX launch footage"]:
        assert router.classify(q) == router.VIDEO, q


def test_exhaustive_wins_over_media():
    # An exhaustive ask that also mentions images must route exhaustive, not image.
    assert router.classify("give me a comprehensive overview with images of Hawaii") == router.EXHAUSTIVE
    assert router.classify("research everything about deep sea vents") == router.EXHAUSTIVE


def test_word_boundaries_do_not_overtrigger():
    # "imagine" must not read as "image"; "overview" must not read as "view".
    assert router.classify("imagine a better tax system") == router.TEXT
    assert router.classify("give me an overview of the economy") == router.TEXT


def test_category_mapping():
    assert router.searxng_category(router.IMAGE) == "images"
    assert router.searxng_category(router.VIDEO) == "videos"
    assert router.searxng_category(router.TEXT) == "general"
    assert router.searxng_category(router.EXHAUSTIVE) == "general"


def test_empty_query():
    assert router.classify("") == router.TEXT
    assert router.classify(None) == router.TEXT
