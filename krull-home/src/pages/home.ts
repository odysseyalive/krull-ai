import { Header } from "../components/Header";
import { Nav } from "../components/Nav";
import { ServiceCard } from "../components/ServiceCard";
import { fetchServices, type ServiceStatus } from "../lib/api";

const SERVICE_COPY: Record<string, { description: string; glyph: string }> = {
  "krull-webui": {
    description:
      "Chat with local language models. Open WebUI with web search, knowledge lookup, and tool calling.",
    glyph: "✶",
  },
  "krull-map-viewer": {
    description:
      "Offline maps with terrain, nautical charts, and aeronautical sectionals. Search by place name.",
    glyph: "◈",
  },
  "krull-kiwix": {
    description:
      "Wikipedia and the Library of Alexandria, served from local ZIM files. Searchable and offline.",
    glyph: "❦",
  },
};

const SECONDARY_LINKS: Array<{ href: string; label: string; hint: string }> = [
  {
    href: "/library",
    label: "Library of Alexandria",
    hint: "Browse and install knowledge packages",
  },
  {
    href: "/settings",
    label: "Settings",
    hint: "Edit .env and restart services",
  },
  {
    href: "/about",
    label: "About Krull",
    hint: "Why this exists",
  },
];

export async function HomePage(): Promise<HTMLElement> {
  const root = document.createElement("div");
  root.className = "page page--home";

  root.append(Nav("/"));
  root.append(
    Header({
      image: "/images/headers/home.webp",
      eyebrow: "Krull · Home",
      title: "The Library of Alexandria, rebuilt offline.",
      subtitle:
        "Your self-hosted portal to AI, maps, and the sum of human knowledge — running entirely on your own machine.",
    }),
  );

  // ----- main services -----
  const main = document.createElement("section");
  main.className = "section section--services";

  const sectionHead = document.createElement("div");
  sectionHead.className = "section__head";
  const eyebrow = document.createElement("p");
  eyebrow.className = "section__eyebrow";
  eyebrow.textContent = "Three doors";
  const sectionTitle = document.createElement("h2");
  sectionTitle.className = "section__title";
  sectionTitle.textContent = "Where would you like to go?";
  sectionHead.append(eyebrow, sectionTitle);
  main.append(sectionHead);

  const grid = document.createElement("div");
  grid.className = "service-grid";
  grid.setAttribute("aria-busy", "true");
  main.append(grid);

  // Render placeholders immediately so the page never flashes empty.
  const placeholders = (Object.keys(SERVICE_COPY)).map((container) => {
    const stub: ServiceStatus = {
      name: container.replace("krull-", "").replace("-", " "),
      container,
      url: "#",
      state: "unknown",
    };
    return ServiceCard({
      status: stub,
      description: SERVICE_COPY[container]?.description ?? "",
      glyph: SERVICE_COPY[container]?.glyph ?? "·",
    });
  });
  grid.append(...placeholders);

  // ----- secondary links -----
  const secondary = document.createElement("section");
  secondary.className = "section section--secondary";
  const secList = document.createElement("ul");
  secList.className = "secondary-links";
  for (const link of SECONDARY_LINKS) {
    const li = document.createElement("li");
    const a = document.createElement("a");
    a.href = link.href;
    a.className = "secondary-link";
    const label = document.createElement("span");
    label.className = "secondary-link__label";
    label.textContent = link.label;
    const hint = document.createElement("span");
    hint.className = "secondary-link__hint";
    hint.textContent = link.hint;
    a.append(label, hint);
    li.append(a);
    secList.append(li);
  }
  secondary.append(secList);

  root.append(main, secondary);

  // Live service health: fetch after first paint, then re-poll periodically.
  const refresh = async () => {
    try {
      const services = await fetchServices();
      grid.replaceChildren(
        ...services.map((s) =>
          ServiceCard({
            status: s,
            description: SERVICE_COPY[s.container]?.description ?? "",
            glyph: SERVICE_COPY[s.container]?.glyph ?? "·",
          }),
        ),
      );
      grid.removeAttribute("aria-busy");
    } catch {
      grid.removeAttribute("aria-busy");
    }
  };
  void refresh();
  const interval = window.setInterval(refresh, 8000);
  // Stop polling when navigating away.
  window.addEventListener(
    "popstate",
    () => window.clearInterval(interval),
    { once: true },
  );

  return root;
}
