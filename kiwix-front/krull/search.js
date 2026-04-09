// Repoints the Kiwix welcome page's search box from its stock
// library-metadata filter (name="q", handled client-side by filtering
// the list of books by title/lang) to a real full-text content search
// across every loaded English ZIM via /search?pattern=...
//
// Why this file exists:
//   The stock welcome page form has no action attribute and the
//   welcome page JS installs `.onsubmit = e => e.preventDefault()`
//   during init (see kiwix-serve skin/index.js near "kiwixSearchForm").
//   So simply rewriting the form markup via sub_filter would be undone
//   at runtime. Instead, we let the stock JS wire up its filter, then
//   override at the event-dispatch level.
//
// Why the book list is enumerated from the catalog:
//   Kiwix rejects multi-book /search requests whose participating books
//   disagree on language ("confusion of tongues", HTTP 400). The OPDS
//   catalog's ?lang=eng filter matches any book whose language list
//   *contains* eng — including TED multilingual ZIMs tagged with a
//   comma-separated language list. Those poison the search scope. We
//   fetch the catalog once, keep only entries whose <language> is
//   strictly "eng" (no commas), and pass each book explicitly as
//   books.name=... — this is the only scoping kiwix-serve accepts that
//   spans the whole English corpus without tripping the language check.
(function () {
  // Kick off the catalog fetch immediately on script load so by the
  // time the user types + presses Enter, the book list is ready.
  var booksPromise = fetch("/catalog/v2/entries?lang=eng&count=500", {
    credentials: "same-origin",
  })
    .then(function (r) {
      if (!r.ok) throw new Error("catalog fetch failed: " + r.status);
      return r.text();
    })
    .then(parseMonoEngBooks)
    .catch(function (err) {
      console.warn("[krull search] catalog fetch failed:", err);
      return [];
    });

  function parseMonoEngBooks(xml) {
    // Split on <entry> and keep only ones with a mono-eng <language>.
    // Simpler than a proper DOM parse and avoids namespace headaches
    // (kiwix emits <language> in the default Atom namespace in the
    // entries feed, but <dc:language> in the languages feed — DOM
    // namespace handling between the two is inconsistent).
    var chunks = xml.split(/<entry[>\s]/).slice(1);
    var out = [];
    for (var i = 0; i < chunks.length; i++) {
      var chunk = chunks[i];
      var lang = chunk.match(/<language>([^<]+)<\/language>/);
      if (!lang || lang[1].trim() !== "eng") continue;
      var href = chunk.match(/href="\/content\/([^/"]+)"/);
      if (href) out.push(href[1]);
    }
    return out;
  }

  // Strip the stock library-filter's "q" param from the hash AND from
  // the stock "filters" cookie.
  //
  // Stock behavior (see kiwix-serve skin/index.js):
  //   - On every change event, resetAndFilter() does pushState(#q=X)
  //     AND setCookie("filters", "q=X", oneDayDelta).
  //   - On load, `params = new FragmentParams(location.hash || filters)`
  //     — so the cookie is consulted as a fallback when no hash is
  //     present, which means a fresh visit to / still replays q=X
  //     and filters the library to "No result" for a word that was
  //     never a book title in the first place.
  //
  // We clean both sides (hash + cookie). Other filters (lang, category,
  // tag) are preserved so the user's language/category selection
  // survives a round trip.
  function stripQFromParams(paramsStr) {
    var params = new URLSearchParams(paramsStr || "");
    if (!params.has("q")) return null;
    params.delete("q");
    return params.toString();
  }

  function clearQFromHash() {
    if (!(window.history && window.history.replaceState)) return;
    var frag = window.location.hash || "";
    if (frag[0] === "#") frag = frag.substring(1);
    var rest = stripQFromParams(frag);
    if (rest === null) return;
    var clean = window.location.pathname + window.location.search +
      (rest ? "#" + rest : "");
    window.history.replaceState(null, "", clean);
  }

  function clearQFromCookie() {
    // Cookie name matches `filterCookieName` in the stock index.js.
    var cookies = document.cookie.split("; ");
    var raw = null;
    for (var i = 0; i < cookies.length; i++) {
      if (cookies[i].indexOf("filters=") === 0) {
        raw = cookies[i].substring("filters=".length);
        break;
      }
    }
    if (raw === null) return;
    var value;
    try {
      value = decodeURIComponent(raw);
    } catch (e) {
      value = raw;
    }
    var rest = stripQFromParams(value);
    if (rest === null) return;
    // Rewrite the cookie with q removed. Match stock's 1-day expiry
    // and site-wide path so the new value fully replaces the old.
    var exp = new Date(Date.now() + 86400000).toUTCString();
    document.cookie = "filters=" + encodeURIComponent(rest) +
      "; expires=" + exp + "; path=/";
  }

  function handleSubmit(e) {
    var form = e.target;
    if (!form || form.id !== "kiwixSearchForm") return;
    var input = document.getElementById("searchFilter");
    var q = input && input.value ? input.value.trim() : "";
    // Stop the stock preventDefault handler from running.
    e.stopImmediatePropagation();
    e.preventDefault();
    if (!q) return;

    // Disable the submit button briefly so a second Enter during the
    // catalog fetch doesn't queue a second navigation.
    var submitBtn = document.getElementById("searchButton");
    if (submitBtn) submitBtn.disabled = true;

    clearQFromHash();
    clearQFromCookie();

    booksPromise.then(function (books) {
      var url = "/search?pattern=" + encodeURIComponent(q);
      for (var i = 0; i < books.length; i++) {
        url += "&books.name=" + encodeURIComponent(books[i]);
      }
      window.location.href = url;
    });
  }
  window.addEventListener("submit", handleSubmit, true);

  // Also clean up on every welcome-page load, not just on submit.
  // A user who ran the stock library filter before this shim was
  // installed still has a 1-day "filters" cookie with q=something
  // on their machine — without this sweep, their first visit after
  // the upgrade would still replay the stale filter from the cookie
  // and look like "everything is missing". Safe because q has no
  // meaning for us anymore; the search box is FTS now.
  function sweepStaleFilter() {
    // Only act on the welcome page, not search results or ZIM content.
    if (!document.getElementById("kiwixSearchForm")) return;
    clearQFromHash();
    clearQFromCookie();
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", sweepStaleFilter);
  } else {
    sweepStaleFilter();
  }

  // Rebrand the placeholder so the change in behavior is discoverable.
  // The stock updateUIText() may run asynchronously after i18n loads
  // and overwrite our placeholder, so we reapply on multiple events.
  function rebrandPlaceholder() {
    var input = document.getElementById("searchFilter");
    if (input) input.placeholder = "Search all content";
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", rebrandPlaceholder);
  } else {
    rebrandPlaceholder();
  }
  window.addEventListener("load", function () {
    rebrandPlaceholder();
    // Belt-and-braces: the stock i18n-driven updateUIText() sometimes
    // resets the placeholder a tick after load. Re-set once more on
    // the next frame.
    requestAnimationFrame(rebrandPlaceholder);
  });
})();
