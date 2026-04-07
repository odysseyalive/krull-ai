import { Header } from "../components/Header";
import { Nav } from "../components/Nav";

interface OfflineRow {
  component: string;
  status: "yes" | "partial" | "no";
  notes: string;
}

const OFFLINE_MATRIX: OfflineRow[] = [
  { component: "LLM inference (Ollama)", status: "yes", notes: "Models run locally on GPU/CPU." },
  { component: "Chat UI (Open WebUI)", status: "yes", notes: "Served locally." },
  { component: "Map viewer", status: "yes", notes: "Tiles served from Martin / PMTiles." },
  { component: "Map search", status: "yes", notes: "Place index + Photon geocoding, all local." },
  { component: "Geocoding (Photon)", status: "yes", notes: "OSM database runs locally." },
  { component: "Wikipedia & knowledge (Kiwix)", status: "yes", notes: "ZIM files served locally." },
  { component: "API gateway (LiteLLM)", status: "partial", notes: "Works for local models. Cloud APIs fail (and that's the point)." },
  { component: "Web search (SearXNG)", status: "no", notes: "Returns empty results gracefully — nothing crashes." },
];

const STATUS_LABEL: Record<OfflineRow["status"], string> = {
  yes: "Offline",
  partial: "Partial",
  no: "Online",
};

export async function AboutPage(): Promise<HTMLElement> {
  const root = document.createElement("div");
  root.className = "page page--about";
  root.append(Nav("/about"));
  root.append(
    Header({
      image: "/images/headers/about.webp",
      eyebrow: "About",
      title: "Why Krull exists.",
      subtitle:
        "An offline-first AI workstation built for the long power-out — and for anyone who doesn't want their tools dependent on someone else's servers.",
    }),
  );

  const section = document.createElement("section");
  section.className = "section section--about";

  // Manifesto blocks
  section.append(
    block(
      "Knowledge survives the power-out.",
      "Krull bundles a local language model, a beautiful map of the world, and the sum of human knowledge — Wikipedia, Project Gutenberg, Stack Exchange, dev docs, survival manuals — into a single self-hosted workstation that runs on your hardware. No cloud accounts. No API keys. No internet required after setup.",
    ),
    block(
      "It still works when nothing else does.",
      "When the network is down, when the company is gone, when the API has been turned off, when the rain is sideways and the power is flickering — Krull keeps working. Local models answer questions. Local maps still show every road. Local Wikipedia still tells you how to splint a leg, ferment cabbage, identify a snake, or rebuild a carburetor.",
    ),
    block(
      "Built for Claude Code, on your machine.",
      "Krull plugs into Claude Code through a LiteLLM gateway and a small SSE proxy. Your existing skills, hooks, CLAUDE.md files, and workflows keep working — your brain is just local now.",
    ),
  );

  // Offline matrix
  const matrix = document.createElement("div");
  matrix.className = "offline-matrix";
  const heading = document.createElement("h3");
  heading.className = "kind-panel__heading";
  heading.textContent = "What works offline";
  matrix.append(heading);

  const list = document.createElement("ul");
  list.className = "offline-matrix__list";
  for (const row of OFFLINE_MATRIX) {
    const li = document.createElement("li");
    li.className = `offline-row offline-row--${row.status}`;
    const dot = document.createElement("span");
    dot.className = "offline-row__dot";
    dot.setAttribute("aria-hidden", "true");
    const name = document.createElement("span");
    name.className = "offline-row__name";
    name.textContent = row.component;
    const status = document.createElement("span");
    status.className = "offline-row__status";
    status.textContent = STATUS_LABEL[row.status];
    const notes = document.createElement("span");
    notes.className = "offline-row__notes";
    notes.textContent = row.notes;
    li.append(dot, name, status, notes);
    list.append(li);
  }
  matrix.append(list);
  section.append(matrix);

  // Closing line
  const closing = document.createElement("p");
  closing.className = "about-closing";
  closing.textContent =
    "Krull is named for the warden of a fortress at the end of the world. Yours holds the library inside.";
  section.append(closing);

  root.append(section);
  return root;
}

function block(title: string, body: string): HTMLElement {
  const wrap = document.createElement("section");
  wrap.className = "about-block";
  const h = document.createElement("h2");
  h.className = "about-block__title";
  h.textContent = title;
  const p = document.createElement("p");
  p.className = "about-block__body";
  p.textContent = body;
  wrap.append(h, p);
  return wrap;
}
