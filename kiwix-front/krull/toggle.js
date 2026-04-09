/*
 * Krull theme toggle for kiwix-serve.
 *
 * The dark class is already set on <html> by an inline script in <head>
 * (see nginx.conf sub_filter) so the page renders dark from the very
 * first paint, no FOUC. This script just adds the toggle button and
 * wires up the click handler.
 *
 * Suppressed inside iframes — kiwix renders article content in an
 * inner frame and we don't want a duplicate button floating there.
 */
(function () {
  if (window.top !== window.self) return; // skip iframes

  function ready(fn) {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", fn, { once: true });
    } else {
      fn();
    }
  }

  ready(function () {
    if (document.getElementById("krull-theme-toggle")) return;

    var btn = document.createElement("button");
    btn.id = "krull-theme-toggle";
    btn.type = "button";
    btn.title = "Toggle theme";
    btn.setAttribute("aria-label", "Toggle dark and light theme");

    function syncIcon() {
      var dark = document.documentElement.classList.contains("krull-dark");
      btn.textContent = dark ? "☀" : "☾";
    }
    syncIcon();

    btn.addEventListener("click", function () {
      var html = document.documentElement;
      var goingDark = !html.classList.contains("krull-dark");
      html.classList.toggle("krull-dark", goingDark);
      html.classList.toggle("krull-light", !goingDark);
      try {
        localStorage.setItem("krull-theme", goingDark ? "dark" : "light");
      } catch (e) {
        /* localStorage may be unavailable in some contexts */
      }
      syncIcon();
    });

    document.body.appendChild(btn);
  });
})();
