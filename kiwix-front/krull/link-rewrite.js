// Rewrites external Wikimedia and Stack Exchange links inside ZIM
// content to point at the local Kiwix instance when the corresponding
// ZIM is loaded. Without this, clicking "Iranian Persian" in the
// Wiktionary article for "Iran" opens https://en.wikipedia.org — even
// though we have wikipedia_en locally.
//
// Also improves the quality of Kiwix search snippets injected into
// Open WebUI chats: the model sees local URLs in the content instead
// of external ones, so its citations point to the offline library.
//
// This script is injected by nginx sub_filter into every HTML response
// from kiwix-serve (same <head> injection as dark.css, search.js, and
// format-rewrite.js). It runs in both the shell and iframe contexts.
//
// On first run it fetches the OPDS catalog (/catalog/v2/entries) to
// discover which ZIMs are loaded, builds a domain→content-path map,
// then rewrites all matching <a> hrefs. A MutationObserver catches
// links added dynamically after initial parse.

(function () {
  "use strict";

  // Only run on pages that contain ZIM content — either iframed
  // inside the viewer shell, or loaded directly at /content/... URLs.
  // Skip the library welcome page and the viewer shell itself, whose
  // links are already local.
  var isIframed = window.self !== window.top;
  var isContentPage = /^\/content\//.test(window.location.pathname);
  if (!isIframed && !isContentPage) return;

  // ── Domain → ZIM content-path mapping ──────────────────────────────
  //
  // Built from the OPDS catalog on first run. Maps external hostnames
  // to their local /content/<zim-name>/ prefix.
  //
  // Examples after catalog fetch:
  //   "en.wikipedia.org"  → "/content/wikipedia_en_all_maxi_2026-02"
  //   "en.wiktionary.org" → "/content/wiktionary_en_all_nopic_2026-02"
  //   "cooking.stackexchange.com" → "/content/cooking.stackexchange.com_en_all_2026-02"

  let domainMap = null; // null = not yet fetched; {} = fetched (possibly empty)
  let catalogPromise = null;

  // Wikimedia ZIM names follow the pattern: <project>_<lang>_all[_flavour]_<date>
  // External URLs follow: https://<lang>.<project>.org/wiki/<article>
  const WIKIMEDIA_PROJECTS = ["wikipedia", "wiktionary", "wikivoyage"];

  function buildDomainMap(entries) {
    const map = {};
    const ns = "http://www.w3.org/2005/Atom";

    for (const entry of entries) {
      const nameEl = entry.getElementsByTagNameNS(ns, "name")[0];
      const langEl = entry.getElementsByTagNameNS(ns, "language")[0];
      if (!nameEl || !langEl) continue;

      const name = nameEl.textContent.trim();
      const lang = (langEl.textContent || "").trim();

      // Skip multilingual ZIMs
      if (lang.includes(",") || !lang) continue;

      // Find the /content/<full-name> href
      let contentPath = null;
      const links = entry.getElementsByTagNameNS(ns, "link");
      for (const link of links) {
        const href = link.getAttribute("href") || "";
        if (href.startsWith("/content/")) {
          contentPath = href.replace(/\/$/, "");
          break;
        }
      }
      if (!contentPath) continue;

      // Wikimedia projects: <project>_<lang>_all → <lang>.<project>.org
      // ZIM names use ISO 639-1 codes (en, fr, de) but the OPDS
      // <language> field uses ISO 639-3 (eng, fra, deu). Extract
      // the lang code from the ZIM name itself.
      for (const project of WIKIMEDIA_PROJECTS) {
        const prefix = project + "_";
        if (name.startsWith(prefix)) {
          const rest = name.substring(prefix.length);
          const zimLang = rest.split("_")[0]; // e.g. "en" from "en_all"
          if (zimLang && zimLang.length >= 2 && zimLang.length <= 3) {
            const domain = zimLang + "." + project + ".org";
            map[domain] = contentPath;
          }
          break;
        }
      }

      // Stack Exchange: <site>.stackexchange.com_<lang>_all → <site>.stackexchange.com
      const seMatch = name.match(/^([a-z]+\.stackexchange\.com)_/);
      if (seMatch) {
        map[seMatch[1]] = contentPath;
      }
    }

    return map;
  }

  function fetchCatalog() {
    if (catalogPromise) return catalogPromise;

    catalogPromise = fetch("/catalog/v2/entries?count=500")
      .then(function (resp) {
        if (!resp.ok) throw new Error("catalog " + resp.status);
        return resp.text();
      })
      .then(function (xml) {
        const parser = new DOMParser();
        const doc = parser.parseFromString(xml, "application/xml");
        const ns = "http://www.w3.org/2005/Atom";
        const entries = doc.getElementsByTagNameNS(ns, "entry");
        domainMap = buildDomainMap(entries);
      })
      .catch(function () {
        domainMap = {};
      });

    return catalogPromise;
  }

  // ── Link rewriting ────────────────────────────────────────────────

  // Extracts the article path from a Wikimedia URL.
  // https://en.wikipedia.org/wiki/Iranian_Persian → "Iranian_Persian"
  // https://en.wiktionary.org/wiki/word#section   → "word"  (hash preserved separately)
  function wikimediaArticlePath(url) {
    const match = url.pathname.match(/^\/wiki\/(.+)/);
    return match ? match[1] : null;
  }

  // Extracts the question path from a Stack Exchange URL.
  // https://cooking.stackexchange.com/questions/27566/how-to-... → "questions/27566/how-to-..."
  // https://cooking.stackexchange.com/a/12345 → "a/12345"
  function stackExchangePath(url) {
    // Keep the full path minus leading slash
    const path = url.pathname.replace(/^\//, "");
    return path || null;
  }

  function rewriteLink(anchor) {
    if (!domainMap || !anchor.href) return;

    let url;
    try {
      url = new URL(anchor.href);
    } catch (_) {
      return;
    }

    // Only rewrite https/http external links
    if (url.protocol !== "https:" && url.protocol !== "http:") return;

    const host = url.hostname;
    const contentBase = domainMap[host];
    if (!contentBase) return;

    // Determine the article/page path based on the domain type
    let articlePath = null;

    if (WIKIMEDIA_PROJECTS.some(function (p) { return host.endsWith("." + p + ".org"); })) {
      articlePath = wikimediaArticlePath(url);
    } else if (host.endsWith(".stackexchange.com")) {
      articlePath = stackExchangePath(url);
    }

    if (!articlePath) return;

    // Build the local URL, preserving any hash fragment
    const localPath = contentBase + "/" + articlePath;
    const hash = url.hash || "";

    anchor.href = localPath + hash;
    // Visual indicator: dim external-link icon that Wiktionary/Wikipedia add
    anchor.classList.add("krull-local-link");
    // Remove the external-link icon class if present (MediaWiki)
    anchor.classList.remove("external");
  }

  function rewriteAllLinks(root) {
    if (!domainMap) return;
    const anchors = (root || document).querySelectorAll("a[href]");
    for (const a of anchors) {
      rewriteLink(a);
    }
  }

  // ── Bootstrap ─────────────────────────────────────────────────────

  function init() {
    fetchCatalog().then(function () {
      rewriteAllLinks(document);

      // Catch links added dynamically (e.g. lazy-loaded sections)
      const observer = new MutationObserver(function (mutations) {
        for (const m of mutations) {
          for (const node of m.addedNodes) {
            if (node.nodeType !== 1) continue;
            if (node.tagName === "A") {
              rewriteLink(node);
            } else if (node.querySelectorAll) {
              const links = node.querySelectorAll("a[href]");
              for (const a of links) {
                rewriteLink(a);
              }
            }
          }
        }
      });
      observer.observe(document.body, { childList: true, subtree: true });
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
