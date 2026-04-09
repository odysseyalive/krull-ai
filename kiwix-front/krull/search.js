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

    booksPromise.then(function (books) {
      var url = "/search?pattern=" + encodeURIComponent(q);
      for (var i = 0; i < books.length; i++) {
        url += "&books.name=" + encodeURIComponent(books[i]);
      }
      window.location.href = url;
    });
  }
  window.addEventListener("submit", handleSubmit, true);

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
