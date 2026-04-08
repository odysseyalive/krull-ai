import { fetchModels, type ModelsPayload } from "../lib/api";

/**
 * "No models installed" warning strip rendered below each page's Nav.
 *
 * Krull is useless without a model pulled, but nothing currently
 * guides a brand-new user from `/` or `/library` to `/settings`.
 * This banner closes that loop: it fetches the model list once per
 * page load, and if the host has zero models pulled it renders a
 * dismissable strip with a link to the settings page.
 *
 * Notes:
 *   - The banner hides on /settings itself (the user is already in
 *     the right place) and any page whose path is passed in.
 *   - Re-checks on `krull:models-changed` events so the banner
 *     disappears without a page reload as soon as ModelPicker
 *     finishes a pull.
 *   - fetchModels() is shared across banner instances via a
 *     module-level promise so navigating between pages doesn't
 *     hammer the endpoint.
 */

let cachedPromise: Promise<ModelsPayload> | null = null;
let cachedAt = 0;
const CACHE_TTL_MS = 15_000;

function getModels(): Promise<ModelsPayload> {
  const now = Date.now();
  if (!cachedPromise || now - cachedAt > CACHE_TTL_MS) {
    cachedPromise = fetchModels().catch((err) => {
      cachedPromise = null;
      throw err;
    });
    cachedAt = now;
  }
  return cachedPromise;
}

/** Clear the module cache. Called when a pull finishes so the next
 *  banner mount sees fresh data. */
function invalidate() {
  cachedPromise = null;
}

export function NoModelsBanner(currentPath: string): HTMLElement {
  const el = document.createElement("div");
  el.className = "krull-banner krull-banner--warning";
  el.style.display = "none";
  const onSettings = currentPath === "/settings";

  async function check() {
    try {
      const data = await getModels();
      const anyInstalled =
        data.recommended.some((m) => m.installed) || data.other.length > 0;
      if (anyInstalled) {
        el.style.display = "none";
        el.replaceChildren();
        return;
      }
    } catch {
      // If the endpoint is unreachable we stay silent rather than
      // showing a false positive on every page.
      el.style.display = "none";
      return;
    }

    el.style.display = "";
    el.replaceChildren();
    const text = document.createElement("span");
    if (onSettings) {
      // We're on the page that can fix this. Point at the picker
      // below instead of linking back to /settings (circular).
      text.textContent =
        "No models installed yet. Pick one from the recommendations below — Krull can't generate responses until you do.";
      el.append(text);
    } else {
      text.textContent =
        "No models installed yet. Krull can't generate responses until you pull one. ";
      const link = document.createElement("a");
      link.href = "/settings";
      link.textContent = "Pick a brain →";
      link.className = "krull-banner__link";
      el.append(text, link);
    }
  }

  // Kick off the first check asynchronously so the page renders
  // immediately and the banner pops in when the fetch resolves.
  void check();

  // React to pull/delete completions from ModelPicker.
  const onChange = () => {
    invalidate();
    void check();
  };
  window.addEventListener("krull:models-changed", onChange);
  // Clean up the listener when the element is detached from the DOM.
  const observer = new MutationObserver(() => {
    if (!document.body.contains(el)) {
      window.removeEventListener("krull:models-changed", onChange);
      observer.disconnect();
    }
  });
  observer.observe(document.body, { childList: true, subtree: true });

  return el;
}
