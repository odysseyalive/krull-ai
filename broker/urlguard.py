"""
Outbound URL guard for page reads (CWE-918 SSRF denial).

Every URL the broker hands to playwright-mcp's web_fetch passes through
`refusal()` first. /load URLs are request-supplied and /search deepening reads
links a search engine returned, so neither is trusted: without this check a
caller could point the headless browser at krull-ollama, krull-webui, the
docker host (host.docker.internal) or cloud metadata (169.254.169.254) and get
the response read back as "page content".

Mechanism follows the two vetted guards already in the tree:
  - functions/web_fetch.py `_PRIVATE_HOST_RE` / `_extract_urls`: refuse
    localhost and private ranges, and refuse a host with no dot (which is
    what a bare docker service name like `krull-ollama` looks like).
  - ../playwright-mcp src/egress.ts `assertEgressAllowed`: refuse the
    .localhost / .internal / .local suffixes, check IP literals against the
    private / loopback / link-local / CGNAT ranges, and resolve DNS names so a
    public-looking name that points inside is refused too.

It is stricter than web_fetch.py's regex where a browser and Python's urllib
would read the same string differently: legacy IPv4 spellings (127.1,
0x7f.0.0.1, 2130706433), percent-encoded or full-width hosts, and a backslash
before an `@` all resolve to a different host in Chromium than in urlsplit.
Those are refused or normalised before the host is checked.

Residual (stated, not covered here): DNS rebinding between this check and the
browser's own lookup, and redirects to a private host. Both can only be
enforced at the browser, which krull-playwright does when its egress guard is
on (PLAYWRIGHT_MCP_PUBLIC_URL set; see src/egress.ts).

A refusal returns a markers.REASON_* so the caller emits an honest marker;
this module never raises on bad input.
"""

import asyncio
import ipaddress
import logging
import re
import socket
import urllib.parse

import markers

log = logging.getLogger("broker.urlguard")

_BLOCKED_SUFFIXES = (".localhost", ".internal", ".local")
_NUMERIC_LABEL = re.compile(r"^(?:0x[0-9a-f]*|[0-9]+)$", re.IGNORECASE)
# Characters a browser strips, decodes or treats as a separator differently
# from urlsplit. A real page URL never needs them raw.
_AMBIGUOUS = re.compile(r"[\\\s\x00-\x1f\x7f]")
_RESOLVE_TIMEOUT_S = 5.0
# RFC 6052 well-known NAT64 prefix: 64:ff9b::a.b.c.d is a.b.c.d through a
# translator. ipaddress calls it global, so its embedded IPv4 is checked.
_NAT64 = ipaddress.ip_network("64:ff9b::/96")


def _ip_of(host: str):
    """The IP a browser would connect to for a literal host, or None for a
    name. Raises ValueError for an all-numeric host that is not a valid IPv4
    address (Chromium rejects those as invalid URLs)."""
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    labels = host.split(".")
    if all(label and _NUMERIC_LABEL.match(label) for label in labels):
        # inet_aton accepts the same legacy forms the WHATWG URL parser does:
        # short (127.1), hex (0x7f.1), octal (0177.0.0.1) and integer.
        try:
            return ipaddress.ip_address(socket.inet_aton(host))
        except OSError as e:
            raise ValueError(f"invalid numeric host {host!r}") from e
    return None


def _ip_blocked(ip) -> bool:
    if ip.version == 6:
        # Unwrap the IPv6 forms that carry an IPv4 address, so the embedded
        # one is what gets checked: ::ffff:a.b.c.d (mapped), ::a.b.c.d
        # (deprecated IPv4-compatible; also catches :: and ::1, which unwrap
        # to the blocked 0.0.0.0/8) and 64:ff9b::a.b.c.d (NAT64).
        if ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        elif ip.packed[:12] == b"\0" * 12 or ip in _NAT64:
            ip = ipaddress.IPv4Address(ip.packed[12:])
    # is_global is False for loopback, RFC1918, link-local (incl. metadata),
    # CGNAT 100.64/10, ULA fc00::/7, unspecified and reserved ranges.
    # Multicast is "global" to ipaddress but is never a web page.
    return (not ip.is_global) or ip.is_multicast


async def _resolve(host: str) -> list:
    loop = asyncio.get_running_loop()
    infos = await asyncio.wait_for(
        loop.getaddrinfo(host, None, type=socket.SOCK_STREAM), _RESOLVE_TIMEOUT_S
    )
    return [info[4][0] for info in infos]


def _static_refusal(url):
    """Checks that need no network. Returns (reason or None, host)."""
    if not isinstance(url, str) or not url or _AMBIGUOUS.search(url):
        return markers.REASON_BAD_URL, ""
    try:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname or ""
    except ValueError:
        return markers.REASON_BAD_URL, ""
    if parts.scheme.lower() not in ("http", "https") or not host or "%" in host:
        return markers.REASON_BAD_URL, ""
    if not host.isascii():
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            return markers.REASON_BAD_URL, ""
    host = host.lower().rstrip(".")
    if not host:
        return markers.REASON_BAD_URL, ""
    try:
        ip = _ip_of(host)
    except ValueError:
        return markers.REASON_BAD_URL, ""
    if ip is not None:
        return (markers.REASON_PRIVATE_HOST if _ip_blocked(ip) else None), host
    if host == "localhost" or host.endswith(_BLOCKED_SUFFIXES):
        return markers.REASON_PRIVATE_HOST, host
    if "." not in host:
        # A bare name (krull-ollama, krull-webui, ...) only resolves inside
        # the docker network; web_fetch.py refuses it for the same reason.
        return markers.REASON_PRIVATE_HOST, host
    return None, host


async def refusal(url, resolve=None):
    """Return a markers.REASON_* naming why `url` must not be fetched, or None
    when it may be. A name that resolves to ANY non-public address is refused;
    a name that does not resolve is let through, so the fetch fails on its own
    and the caller reports that failure honestly (as egress.ts does)."""
    reason, host = _static_refusal(url)
    if reason or _ip_of(host) is not None:
        return reason
    try:
        addrs = await (resolve or _resolve)(host)
    except (OSError, asyncio.TimeoutError, UnicodeError) as e:
        log.info("urlguard: %s did not resolve (%r); fetch will report its own failure", host, e)
        return None
    for addr in addrs:
        # getaddrinfo returns numeric addresses; a v6 one may carry a %scope.
        if _ip_blocked(ipaddress.ip_address(addr.split("%", 1)[0])):
            log.info("urlguard: %s resolves to non-public %s -> refused", host, addr)
            return markers.REASON_PRIVATE_HOST
    return None
