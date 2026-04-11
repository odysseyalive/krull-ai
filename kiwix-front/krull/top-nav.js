// Persistent top navigation bar for Krull kiwix pages that don't
// have kiwix's welcome chrome — content pages (/content/<zim>/<path>),
// search result pages (/search?pattern=...), and kiwix's raw
// "Invalid request" error page. Injects a fixed header with a
// Krull mark + full-library search box, handles submit through
// the shared OPDS-scoped URL builder from search.js, and swaps
// kiwix's terse 400 error page for a friendly card the user can
// actually recover from.
//
// Load order: this script loads after search.js (same defer head
// injection, document order) so window.__krullSearch is always
// populated by the time DOMContentLoaded fires.
//
// The welcome page is skipped — it already has its own .kiwixNav
// that search.js hijacks, and stacking two search bars there would
// just confuse the user.

(function () {
  "use strict";

  function el(tag, props, children) {
    var node = document.createElement(tag);
    if (props) {
      for (var k in props) {
        if (!Object.prototype.hasOwnProperty.call(props, k)) continue;
        if (k === "class") node.className = props[k];
        else if (k === "text") node.textContent = props[k];
        else node.setAttribute(k, props[k]);
      }
    }
    if (children) {
      for (var i = 0; i < children.length; i++) {
        if (children[i]) node.appendChild(children[i]);
      }
    }
    return node;
  }

  // The welcome library index has its own .kiwixNav (filter + search)
  // that search.js already hijacks — we don't want to stack a second
  // search bar on top. Detect by the stock form id being present at
  // DOM-ready time.
  function isWelcomePage() {
    return !!document.getElementById("kiwixSearchForm");
  }

  // Render a branded hero section above the stock .kiwixNav on the
  // welcome page: serif Krull wordmark + tagline. Gives the library
  // an identity without disturbing the functional filter nav below.
  function renderWelcomeHero() {
    var nav = document.querySelector(".kiwixNav");
    if (!nav) return;
    // Don't double-inject on hash changes or language switches.
    if (document.querySelector(".krull-hero")) return;

    var mark = el("div", {
      "class": "krull-hero__mark",
      text: "Krull",
    });
    var tagline = el("div", {
      "class": "krull-hero__tagline",
      text: "an offline library",
    });
    var hero = el("section", { "class": "krull-hero" }, [mark, tagline]);
    nav.parentNode.insertBefore(hero, nav);
  }

  function buildTopNav() {
    var mark = el("a", {
      href: "/",
      "class": "krull-topnav__mark",
      text: "Krull",
    });

    var input = el("input", {
      type: "text",
      name: "q",
      id: "searchFilter",
      "class": "kiwixSearch krull-topnav__input",
      placeholder: "Search all content",
      autocomplete: "off",
    });

    var submit = el("input", {
      type: "submit",
      id: "searchButton",
      "class": "kiwixButton krull-topnav__submit",
      value: "Search",
    });

    var form = el(
      "form",
      {
        id: "kiwixSearchForm",
        "class": "kiwixNav__SearchForm krull-topnav__form",
      },
      [input, submit]
    );

    return el(
      "header",
      { "class": "krull-topnav kiwixNav" },
      [mark, form]
    );
  }

  function buildOverlay() {
    var spinner = el("div", {
      "class": "krull-spinner",
      "aria-hidden": "true",
    });
    var caption = el("div", {
      "class": "krull-search-overlay__caption",
      text: "Searching the library…",
    });
    return el(
      "div",
      { "class": "krull-search-overlay" },
      [spinner, caption]
    );
  }

  // Kiwix's error page has title "Invalid request" and an <h1>
  // with the same text, no classes. Detect conservatively: both
  // signals must match before we rewrite the page, so a legitimate
  // article that happens to share one of those strings never gets
  // clobbered.
  function isErrorPage() {
    if (document.title !== "Invalid request") return false;
    var h1 = document.body && document.body.querySelector("h1");
    if (!h1) return false;
    return (h1.textContent || "").trim() === "Invalid request";
  }

  function renderErrorCard() {
    // Pull the detail text kiwix put in the second <p> so we can
    // show the actual reason (e.g., "Two or more books in different
    // languages…") in a muted subline. Fall back to a generic line
    // if the shape changed.
    var ps = document.body.querySelectorAll("p");
    var detail = "";
    if (ps.length >= 2 && ps[1].textContent) {
      detail = ps[1].textContent.trim();
    }

    // Remove everything in the body that isn't our header.
    var header = document.body.querySelector(".krull-topnav");
    while (document.body.firstChild) {
      document.body.removeChild(document.body.firstChild);
    }
    if (header) document.body.appendChild(header);

    var mark = el("div", {
      "class": "krull-error__mark",
      "aria-hidden": "true",
      text: "⌘",
    });
    var title = el("h1", {
      "class": "krull-error__title",
      text: "That search didn't land",
    });
    var detailP = el("p", {
      "class": "krull-error__detail",
      text: detail || "The search couldn't be completed.",
    });

    // Hint line with an embedded link — built from nodes so we
    // don't need innerHTML.
    var hint = el("p", { "class": "krull-error__hint" });
    hint.appendChild(
      document.createTextNode(
        "Try a different query from the bar above, or "
      )
    );
    hint.appendChild(el("a", { href: "/", text: "return to the library" }));
    hint.appendChild(document.createTextNode("."));

    var card = el(
      "div",
      { "class": "krull-error" },
      [mark, title, detailP, hint]
    );
    document.body.appendChild(card);

    // Clear the error title so it doesn't linger in the tab.
    document.title = "Krull — no results";
  }

  function handleSubmit(header, overlay) {
    header.querySelector("#kiwixSearchForm").addEventListener(
      "submit",
      function (e) {
        e.preventDefault();
        var input = header.querySelector("#searchFilter");
        var q = input && input.value ? input.value.trim() : "";
        if (!q) return;
        if (!window.__krullSearch || !window.__krullSearch.buildSearchUrl) {
          // search.js hasn't loaded — fall back to an unscoped
          // search rather than freezing. Kiwix may still return
          // the 400 error page, but at least we try.
          window.location.href = "/search?pattern=" + encodeURIComponent(q);
          return;
        }
        overlay.classList.add("is-visible");
        window.__krullSearch.buildSearchUrl(q).then(function (url) {
          window.location.href = url;
        });
      },
      true
    );
  }

  function tagSearchPage() {
    // Historically we added a .krull-search-page class to <body> at
    // DOMContentLoaded so CSS could scope the search-results polish.
    // That caused a FOUC — the raw search results would flash for
    // one frame before the class applied. We now target the page
    // via `body:has(> .results)` in dark.css, which matches from
    // first paint. This function is kept as a no-op for backward
    // compatibility with any callers that reference it.
  }

  function init() {
    // Always mount the loading overlay, on every page — including
    // the welcome page. search.js looks it up by class to show it
    // during the submit→navigate window so the user sees purposeful
    // feedback instead of a frozen page or a flash of the next one.
    var overlay = buildOverlay();
    document.body.appendChild(overlay);

    if (isWelcomePage()) {
      renderWelcomeHero();
      return;
    }

    var header = buildTopNav();

    // Insert at the very top of <body> so fixed positioning + the
    // body padding-top in dark.css yields a predictable layout
    // regardless of the inner ZIM's own wrappers.
    if (document.body.firstChild) {
      document.body.insertBefore(header, document.body.firstChild);
    } else {
      document.body.appendChild(header);
    }

    handleSubmit(header, overlay);
    tagSearchPage();

    if (isErrorPage()) {
      renderErrorCard();
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
