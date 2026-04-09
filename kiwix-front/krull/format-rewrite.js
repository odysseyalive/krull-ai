// Routes binary-format URLs (.epub, .pdf) inside the Kiwix viewer to
// something the user can actually read inside the iframe instead of
// triggering a download.
//
// Two cases:
//
//   1. Gutenberg-style ZIMs (gutenberg_en_lcc-*) ship every book as
//      BOTH a downloadable EPUB at  `<title>.<id>.epub` AND an authored
//      HTML rendering at the very same path with the `.epub` suffix
//      stripped: `<title>.<id>` (verified: HEAD returns 200 text/html).
//      The book "info" / cover page exposes both as separate icons but
//      the EPUB icon is the visual default and clicking it makes the
//      browser download a binary, leaving the viewer iframe blank. We
//      rewrite those links to the HTML sibling so the icon Just Works.
//
//   2. Any other ZIM that contains a bare `.epub` or `.pdf` entry with
//      no HTML sibling — we route the link through the krull reader
//      wrapper pages (`/krull/epub-reader.html`, `/krull/pdf-reader.html`)
//      which load epub.js / pdf.js against the binary URL and render
//      it inside the viewer iframe with the Krull dark palette.
//
// This script is injected by nginx into BOTH the viewer shell
// (`/viewer`) and every iframed ZIM HTML response — same `<head>`
// sub_filter as dark.css. We branch on `window.self === window.top`
// to know which context we're in:
//
//   - In the SHELL: hook hashchange + initial load, rewrite the hash
//     before viewer.js calls iframe.src = ... so a direct deep-link to
//     `#book/path.epub` resolves to the readable variant.
//
//   - In the IFRAME (book content): walk a[href] on DOMContentLoaded
//     and rewrite in place so a click on the EPUB icon goes straight
//     to the HTML sibling — no hashchange round trip, no flash of
//     binary download attempt.
(function () {
  // Gutenberg pattern: any path ending in `.<digits>.epub` is a
  // Gutenberg book. The digits are the Project Gutenberg book ID and
  // the same path without `.epub` is the authored HTML. We require
  // the digit suffix specifically — generic `.epub` files in other
  // ZIMs (no numeric ID) fall through to the wrapper path.
  var GUTENBERG_EPUB = /^(.+\.\d+)\.epub(.*)$/;

  // Generic binary suffix we can hand to the reader wrappers.
  var BINARY_SUFFIX = /\.(epub|pdf)(?:[?#].*)?$/i;

  // Build the wrapper URL for a binary content URL. `contentUrl` is an
  // absolute path under `/content/...` (or whatever the user provided).
  function wrapperFor(contentUrl) {
    var ext = (contentUrl.match(/\.(epub|pdf)(?:[?#]|$)/i) || [])[1];
    if (!ext) return null;
    var reader = ext.toLowerCase() === "pdf"
      ? "/krull/pdf-reader.html"
      : "/krull/epub-reader.html";
    return reader + "?src=" + encodeURIComponent(contentUrl);
  }

  // ---- Shell context: rewrite the viewer's hash before iframe load -----
  //
  // The viewer reads `location.hash` and assigns
  //   iframe.src = "./content/" + hash.slice(1)
  // We get to run first because we're injected as a defer script in the
  // <head>, and viewer.js's setupViewer() runs from `body onload` which
  // fires AFTER all defer scripts. So mutating location.hash here is
  // sufficient to redirect the very first iframe load.
  function rewriteHashIfNeeded() {
    var raw = window.location.hash;
    if (!raw || raw.length < 2) return false;
    // Strip the leading `#`. The hash is the ZIM-relative path: e.g.
    // `gutenberg_en_lcc-l_2026-03/Book.24082.epub`.
    var rest = raw.substring(1);

    // Case 1: Gutenberg `*.<id>.epub` → strip the suffix.
    var m = rest.match(GUTENBERG_EPUB);
    if (m) {
      var clean = "#" + m[1] + (m[2] || "");
      if (clean !== raw) {
        // replaceState avoids a back-button trap on the broken URL.
        window.history.replaceState(null, "", clean);
        return true;
      }
      return false;
    }

    // Case 2: any other binary suffix → escape to the wrapper page.
    if (BINARY_SUFFIX.test(rest)) {
      var contentUrl = "/content/" + rest;
      var wrap = wrapperFor(contentUrl);
      if (wrap) {
        // Full navigation away from the viewer shell — the wrapper
        // owns the whole tab from this point on.
        window.location.replace(wrap);
        return true;
      }
    }
    return false;
  }

  // ---- Iframe context: rewrite anchor hrefs in book content ------------
  //
  // Same logic but applied to `<a href>` attributes. The Gutenberg
  // cover page has two `<a title="...: EPUB|HTML">` icons; we rewrite
  // the EPUB icon's href to point at the HTML sibling so the click
  // never traverses the binary URL at all.
  function rewriteAnchors(doc) {
    var links = doc.querySelectorAll("a[href]");
    for (var i = 0; i < links.length; i++) {
      var a = links[i];
      var href = a.getAttribute("href");
      if (!href) continue;

      // Skip protocol-absolute and external URLs.
      if (/^[a-z]+:/i.test(href) && !/^https?:\/\/[^/]*\/(viewer|content)/i.test(href)) {
        continue;
      }

      // Drop a leading `/viewer#` if present so the regex sees the bare
      // ZIM-relative path; we'll re-attach the prefix on output.
      var prefix = "";
      var path = href;
      var viewerMatch = path.match(/^(\/viewer)?#(.*)$/);
      if (viewerMatch) {
        prefix = (viewerMatch[1] || "") + "#";
        path = viewerMatch[2];
      }

      var m = path.match(GUTENBERG_EPUB);
      if (m) {
        a.setAttribute("href", prefix + m[1] + (m[2] || ""));
        continue;
      }

      // Bare binary URL with no HTML sibling — only rewrite if there's
      // strong evidence it's a Kiwix content link (lives under
      // `/content/` or is a relative path inside a ZIM viewer hash).
      // Otherwise leave it alone — we don't want to touch arbitrary
      // outbound links.
      if (BINARY_SUFFIX.test(path) && (prefix || /^\/content\//.test(path))) {
        // For hash-style refs, we need an absolute /content/ URL to
        // hand to the wrapper. The viewer shell turns `#book/foo.pdf`
        // into `/content/book/foo.pdf` at iframe load time, so we
        // do the same translation up-front here.
        var contentUrl = prefix
          ? "/content/" + path
          : path;
        var wrap = wrapperFor(contentUrl);
        if (wrap) {
          // Reader wrappers escape the viewer entirely — we want a
          // top-level navigation, not a same-iframe load. Strip the
          // viewer prefix so the browser navigates the parent.
          a.setAttribute("href", wrap);
          a.setAttribute("target", "_top");
        }
      }
    }
  }

  if (window.self === window.top) {
    // Shell. Rewrite on initial load AND on every hashchange so a
    // user clicking an unrewritten link from somewhere else still gets
    // the redirect.
    rewriteHashIfNeeded();
    window.addEventListener("hashchange", rewriteHashIfNeeded, false);
  } else {
    // Iframe. Rewrite anchors as soon as the DOM is parsed.
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", function () {
        rewriteAnchors(document);
      });
    } else {
      rewriteAnchors(document);
    }
  }
})();
