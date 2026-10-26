"""Tests for the /load SSRF guard (urlguard.py), the /load URL cap and the
constant-time bearer check in app.py. Offline: DNS is replaced by a fake
resolver and playwright-mcp by a recording stub, so nothing leaves the host."""

import asyncio
import json
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# The host running these tests may not have the `mcp` SDK (it is a container
# dependency). app.py only needs the two names mcp_client imports from it, and
# every test below replaces the MCP client with a stub, so a placeholder module
# is enough to import app.py. A real install is used untouched when present.
try:
    import mcp.client.streamable_http  # noqa: F401
except ImportError:
    _mcp = types.ModuleType("mcp")
    _mcp.ClientSession = object
    _client = types.ModuleType("mcp.client")
    _sh = types.ModuleType("mcp.client.streamable_http")
    _sh.streamablehttp_client = None
    sys.modules.update({"mcp": _mcp, "mcp.client": _client, "mcp.client.streamable_http": _sh})

import app  # noqa: E402
import markers  # noqa: E402
import urlguard  # noqa: E402


def _resolver(table):
    async def resolve(host):
        if host not in table:
            raise OSError(f"no such host {host}")
        return table[host]

    return resolve


PUBLIC = _resolver({"example.com": ["93.184.216.34", "2606:2800:220:1::1"]})


def _refusal(url, resolve=PUBLIC):
    return asyncio.run(urlguard.refusal(url, resolve=resolve))


def test_private_and_internal_hosts_are_refused():
    for url in [
        "http://127.0.0.1:11434/api/tags",
        "http://localhost:8080/",
        "http://localhost./",
        "http://10.0.0.5/",
        "http://192.168.1.1/admin",
        "http://172.16.0.1/",
        "http://172.31.255.255/",
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        "http://100.64.0.1/",  # CGNAT
        "http://0.0.0.0:8130/",
        "http://[::1]:8765/mcp",
        "http://[fd00::1]/",
        "http://[fe80::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://[::127.0.0.1]/",  # IPv4-compatible (deprecated) loopback
        "http://[::a9fe:a9fe]/",  # IPv4-compatible 169.254.169.254
        "http://[64:ff9b::7f00:1]/",  # NAT64-embedded 127.0.0.1
        "http://[64:ff9b::10.0.0.5]/",  # NAT64-embedded RFC1918
        "http://[2002:7f00:1::]/",  # 6to4 wrapping 127.0.0.1 (container runs 3.12)
        "http://[2001:0:4136:e378:8000:63bf:3fff:fdd2]/",  # Teredo
        "http://[::]/",
        "http://krull-ollama:11434/",  # bare docker service name
        "http://krull-playwright:8765/mcp",
        "http://host.docker.internal:8765/mcp",
        "http://metadata.google.internal/",
        "http://printer.local/",
        "http://app.localhost/",
        "http://127.1/",  # legacy IPv4 spellings a browser accepts
        "http://0x7f.0.0.1/",
        "http://0177.0.0.1/",
        "http://2130706433/",
        "http://１２７．０．０．１/",  # full-width 127.0.0.1
        "http://user@127.0.0.1/",
        "http://224.0.0.1/",  # multicast
    ]:
        assert _refusal(url) == markers.REASON_PRIVATE_HOST, url


def test_malformed_or_non_http_urls_are_refused():
    for url in [
        "file:///etc/passwd",
        "ftp://example.com/x",
        "javascript:alert(1)",
        "http:///nohost",
        "http://127.0.0.1\\@example.com/",  # browser host 127.0.0.1, urlsplit host example.com
        "http://%31%32%37.0.0.1/",  # browser percent-decodes the host
        "http://exa mple.com/",
        "http://999.1.1.1/",  # numeric but not an IPv4 address
        "",
        42,
        None,
        {"url": "https://example.com"},
    ]:
        assert _refusal(url) == markers.REASON_BAD_URL, url


def test_public_hosts_are_allowed():
    assert _refusal("https://example.com/page?q=1") is None
    assert _refusal("https://EXAMPLE.com./") is None
    assert _refusal("http://93.184.216.34/") is None
    assert _refusal("http://[2606:4700:4700::1111]/") is None
    assert _refusal("http://[2001:4860:4860::8888]/") is None
    # The unwrap only checks the embedded IPv4: a public one still passes.
    assert _refusal("http://[64:ff9b::808:808]/") is None  # NAT64 8.8.8.8


def test_public_name_resolving_inside_is_refused():
    rebind = _resolver({"evil.example": ["93.184.216.34", "10.0.0.7"]})
    assert _refusal("https://evil.example/", resolve=rebind) == markers.REASON_PRIVATE_HOST
    nip = _resolver({"127.0.0.1.nip.io": ["127.0.0.1"]})
    assert _refusal("http://127.0.0.1.nip.io/", resolve=nip) == markers.REASON_PRIVATE_HOST


def test_unresolvable_name_is_left_to_the_fetch():
    # The fetch then fails on its own and is reported with its real reason.
    assert _refusal("https://does-not-exist.example/", resolve=_resolver({})) is None


class _FakeRequest:
    def __init__(self, body, headers=None):
        self._body = body
        self.headers = headers or {}

    async def json(self):
        return self._body


class _RecordingMcp:
    def __init__(self):
        self.fetched = []

    async def fetch_page(self, url, allow_solve=True):
        self.fetched.append(url)
        return {
            "fetch_status": "ok",
            "text": f"text of {url}",
            "citation": {"title": "t"},
            "solved": False,
            "solve_attempted": False,
        }


def _load(body, env=None):
    rec = _RecordingMcp()
    saved = (app._mcp, urlguard._resolve, dict(os.environ))
    app._mcp = lambda cfg: rec
    urlguard._resolve = PUBLIC
    os.environ.pop("BROKER_API_KEY", None)
    os.environ.update(env or {})
    try:
        resp = asyncio.run(app.handle_load(_FakeRequest(body)))
    finally:
        app._mcp, urlguard._resolve = saved[0], saved[1]
        os.environ.clear()
        os.environ.update(saved[2])
    return resp.status, json.loads(resp.body), rec.fetched


def test_load_denies_private_host_before_fetch_and_allows_public():
    status, docs, fetched = _load(
        {"urls": ["http://169.254.169.254/latest/meta-data/", "https://example.com/a"]}
    )
    assert status == 200
    # The private URL never reached playwright-mcp; the public one did.
    assert fetched == ["https://example.com/a"]
    assert len(docs) == 2
    denied, allowed = docs
    assert denied["metadata"]["krull_fetch_status"] == "unavailable"
    assert denied["metadata"]["reason"] == markers.REASON_PRIVATE_HOST
    assert "do not fabricate" in denied["page_content"].lower()
    assert allowed["metadata"]["krull_fetch_status"] == "ok"
    assert allowed["page_content"] == "text of https://example.com/a"


def test_load_bad_entries_get_markers_not_errors():
    status, docs, fetched = _load({"urls": [42, "file:///etc/passwd"]})
    assert status == 200 and fetched == []
    assert [d["metadata"]["reason"] for d in docs] == [markers.REASON_BAD_URL] * 2
    # A non-dict JSON body is treated as an empty request, not a 500.
    status, docs, fetched = _load(["https://example.com/"])
    assert status == 200 and docs == [] and fetched == []


def test_load_caps_urls_with_one_honest_marker():
    urls = [f"https://example.com/{i}" for i in range(13)]
    status, docs, fetched = _load({"urls": urls}, env={"BROKER_MAX_LOAD_URLS": "10"})
    assert status == 200
    assert fetched == urls[:10]
    assert len(docs) == 11
    # docs[i] answers urls[i] (Open WebUI may pair them by index); the one
    # over-limit marker comes after every read URL, never before.
    assert [d["metadata"]["source"] for d in docs[:10]] == urls[:10]
    skipped = [d for d in docs if d["metadata"].get("reason") == markers.REASON_OVER_LIMIT]
    assert len(skipped) == 1 and skipped[0] is docs[-1]
    assert skipped[0]["metadata"]["skipped"] == 3
    for u in urls[10:]:
        assert u in skipped[0]["page_content"]


def test_load_keeps_url_order_with_refusals_and_overflow():
    urls = ["https://example.com/a", "http://127.0.0.1/", "https://example.com/c", "https://example.com/d"]
    status, docs, fetched = _load({"urls": urls}, env={"BROKER_MAX_LOAD_URLS": "3"})
    assert status == 200 and fetched == [urls[0], urls[2]]
    assert [d["metadata"]["source"] for d in docs[:3]] == urls[:3]
    assert [d["metadata"].get("reason") for d in docs] == [
        None, markers.REASON_PRIVATE_HOST, None, markers.REASON_OVER_LIMIT
    ]


def test_skipped_marker_is_bounded():
    d = markers.skipped_documents([f"https://e.com/{i}" for i in range(5000)], 10)
    assert "and 4990 more" in d["page_content"]
    assert len(d["page_content"]) < 1000


def test_deepen_never_fetches_a_private_result_link():
    rec = _RecordingMcp()
    saved = (app._mcp, urlguard._resolve)
    app._mcp = lambda cfg: rec
    urlguard._resolve = PUBLIC
    try:
        cfg = app._cfg()
        results = [
            {"link": "http://192.168.0.1/", "title": "router", "snippet": "s1"},
            {"link": "https://example.com/b", "title": "ok", "snippet": "s2"},
        ]
        out = asyncio.run(app._deepen(cfg, results))
    finally:
        app._mcp, urlguard._resolve = saved
    assert rec.fetched == ["https://example.com/b"]
    assert out[0]["snippet"].startswith("s1 [full page not read: it " + markers.REASON_PRIVATE_HOST)
    assert out[1]["snippet"] == "text of https://example.com/b"


def test_authorized_is_constant_time_and_type_safe():
    cfg = {"api_key": "s3cret"}
    ok = _FakeRequest({}, {"Authorization": "Bearer s3cret"})
    assert app._authorized(ok, cfg) is True
    for header in ["Bearer s3cre", "Bearer s3cretX", "", "bearer s3cret", "Bearer s3crét", "\udcff"]:
        assert app._authorized(_FakeRequest({}, {"Authorization": header}), cfg) is False, header
    assert app._authorized(_FakeRequest({}, {}), {"api_key": ""}) is True
    # The comparison really is hmac.compare_digest, not ==.
    import inspect

    assert "hmac.compare_digest" in inspect.getsource(app._authorized)
