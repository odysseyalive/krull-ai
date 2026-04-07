/**
 * Tiny path-based router. Server serves index.html for any path
 * (see server/index.ts catch-all), so we route on location.pathname.
 */
type Renderer = () => HTMLElement | Promise<HTMLElement>;

const routes = new Map<string, Renderer>();

export function defineRoute(path: string, render: Renderer) {
  routes.set(path, render);
}

export async function mountRouter(target: HTMLElement) {
  async function render() {
    const path = window.location.pathname || "/";
    const r = routes.get(path) ?? routes.get("/__notfound__") ?? routes.get("/");
    if (!r) return;
    const node = await r();
    target.replaceChildren(node);
    window.scrollTo({ top: 0, behavior: "instant" as ScrollBehavior });
  }

  // Intercept clicks on internal links so navigation stays SPA.
  document.addEventListener("click", (e) => {
    const target = e.target as HTMLElement | null;
    const a = target?.closest?.("a") as HTMLAnchorElement | null;
    if (!a) return;
    if (a.target === "_blank") return; // external/new-tab links: let through
    const href = a.getAttribute("href");
    if (!href || href.startsWith("http") || href.startsWith("#")) return;
    e.preventDefault();
    if (href !== window.location.pathname) {
      window.history.pushState({}, "", href);
      void render();
    }
  });

  window.addEventListener("popstate", () => void render());
  await render();
}

export function navigate(path: string) {
  if (path === window.location.pathname) return;
  window.history.pushState({}, "", path);
  window.dispatchEvent(new PopStateEvent("popstate"));
}
