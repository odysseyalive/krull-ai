import { Header } from "../components/Header";
import { Nav } from "../components/Nav";
import { PackageRow } from "../components/PackageRow";
import { toast } from "../components/Toast";
import {
  deletePackage,
  fetchCatalog,
  startInstall,
  streamJob,
  type Catalog,
  type CatalogPackage,
  type JobEvent,
  type PackageKind,
} from "../lib/api";

const TABS: Array<{ kind: PackageKind; label: string }> = [
  { kind: "knowledge", label: "Knowledge" },
  { kind: "wikipedia", label: "Wikipedia" },
  { kind: "maps", label: "Maps" },
];

export async function LibraryPage(): Promise<HTMLElement> {
  const root = document.createElement("div");
  root.className = "page page--library";

  root.append(Nav("/library"));
  root.append(
    Header({
      image: "/images/headers/library.webp",
      eyebrow: "Library of Alexandria",
      title: "Every book, every map, every ZIM.",
      subtitle:
        "Browse, install, and curate Krull's offline knowledge collection. Knowledge survives the power-out.",
    }),
  );

  const section = document.createElement("section");
  section.className = "section section--library";
  root.append(section);

  const status = document.createElement("p");
  status.className = "form-status";
  status.textContent = "Loading catalog…";
  section.append(status);

  let catalog: Catalog;
  try {
    catalog = await fetchCatalog();
  } catch (err) {
    status.textContent = `Failed to load catalog: ${(err as Error).message}`;
    return root;
  }
  status.remove();

  // ----- Disk usage strip -----
  const summary = renderSummary(catalog);
  section.append(summary);

  // ----- Tab strip -----
  const tabStrip = document.createElement("div");
  tabStrip.className = "tab-strip";
  tabStrip.setAttribute("role", "tablist");

  const panel = document.createElement("div");
  panel.className = "tab-panel";

  const counts = countByKind(catalog);
  let current: PackageKind = "knowledge";

  const buttons = new Map<PackageKind, HTMLButtonElement>();
  for (const tab of TABS) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "tab";
    btn.setAttribute("role", "tab");
    btn.dataset.kind = tab.kind;
    const labelSpan = document.createElement("span");
    labelSpan.textContent = tab.label;
    const count = document.createElement("span");
    count.className = "tab__count";
    count.textContent = String(counts[tab.kind]);
    btn.append(labelSpan, count);
    btn.addEventListener("click", () => selectTab(tab.kind));
    tabStrip.append(btn);
    buttons.set(tab.kind, btn);
  }

  function selectTab(kind: PackageKind) {
    current = kind;
    for (const [k, b] of buttons) {
      b.setAttribute("aria-selected", k === kind ? "true" : "false");
    }
    renderPanel();
  }

  function renderPanel() {
    panel.replaceChildren(buildKindPanel(catalog, current, handleInstall, handleDelete));
  }

  async function handleInstall(pkg: CatalogPackage) {
    const row = panel.querySelector(
      `.pkg-row[data-key="${cssEscape(pkg.key)}"][data-kind="${pkg.kind}"]`,
    ) as HTMLElement | null;
    if (!row) return;
    const button = row.querySelector("button") as HTMLButtonElement | null;
    const bar = row.querySelector(".pkg-row__progress-bar") as HTMLElement | null;
    row.classList.add("pkg-row--installing");
    if (button) {
      button.disabled = true;
      button.textContent = "Starting…";
    }
    try {
      const { jobId } = await startInstall(pkg.kind, pkg.key);
      const stop = streamJob(jobId, (ev) => {
        applyJobEvent(ev, row, button, bar);
        if (ev.phase === "done") {
          stop();
          toast(`Installed ${pkg.name}.`, "success");
          // Refetch catalog so the row reflects the new "Installed" state.
          void refreshCatalog();
        } else if (ev.phase === "failed") {
          stop();
          toast(`Install failed: ${ev.error ?? "unknown error"}`, "error", 6000);
          row.classList.remove("pkg-row--installing");
          if (button) {
            button.disabled = false;
            button.textContent = "Install";
          }
        }
      });
    } catch (err) {
      toast((err as Error).message, "error", 6000);
      row.classList.remove("pkg-row--installing");
      if (button) {
        button.disabled = false;
        button.textContent = "Install";
      }
    }
  }

  async function handleDelete(pkg: CatalogPackage) {
    if (!confirmInline(pkg)) return;
    try {
      await deletePackage(pkg.kind, pkg.key);
      toast(`Deleted ${pkg.name}.`, "success");
      void refreshCatalog();
    } catch (err) {
      toast(`Delete failed: ${(err as Error).message}`, "error", 6000);
    }
  }

  async function refreshCatalog() {
    try {
      const fresh = await fetchCatalog();
      catalog = fresh;
      // Update summary in place
      const oldSummary = section.querySelector(".library-summary");
      if (oldSummary) oldSummary.replaceWith(renderSummary(catalog));
      renderPanel();
    } catch {
      /* ignore */
    }
  }

  selectTab("knowledge");
  section.append(tabStrip, panel);

  return root;
}

function applyJobEvent(
  ev: JobEvent,
  row: HTMLElement,
  button: HTMLButtonElement | null,
  bar: HTMLElement | null,
) {
  if (bar && typeof ev.percent === "number") {
    bar.style.width = `${ev.percent}%`;
  }
  if (!button) return;
  switch (ev.phase) {
    case "queued":
      button.textContent = "Queued";
      break;
    case "downloading":
      button.textContent = ev.percent != null ? `${ev.percent}%` : "Downloading";
      break;
    case "restarting":
      button.textContent = "Restarting";
      break;
    case "done":
      button.textContent = "Installed";
      break;
    case "failed":
      button.textContent = "Failed";
      break;
  }
}

function confirmInline(pkg: CatalogPackage): boolean {
  // Lightweight confirm. Replaced with a popover in Phase 7's polish pass.
  // We avoid window.confirm because it blocks the event loop and trips Playwright.
  const ok = window.prompt(`Type "delete" to remove ${pkg.name}`);
  return ok === "delete";
}

function cssEscape(s: string): string {
  // Minimal — covers our key format (alnum + dash).
  return s.replace(/"/g, '\\"');
}

function countByKind(catalog: Catalog): Record<PackageKind, number> {
  const counts: Record<PackageKind, number> = {
    knowledge: 0,
    wikipedia: 0,
    maps: 0,
  };
  for (const pkg of catalog.packages) counts[pkg.kind]++;
  return counts;
}

function renderSummary(catalog: Catalog): HTMLElement {
  const installed = catalog.packages.filter((p) => p.installed);
  const totalBytes = installed.reduce(
    (a, p) => a + (p.installedSizeBytes ?? 0),
    0,
  );
  const wrap = document.createElement("div");
  wrap.className = "library-summary";

  const stats: Array<[string, string]> = [
    ["Installed", String(installed.length)],
    ["Total size", formatBytesCompact(totalBytes)],
    ["Available", String(catalog.packages.length)],
  ];

  for (const [label, value] of stats) {
    const stat = document.createElement("div");
    stat.className = "library-summary__stat";
    const v = document.createElement("div");
    v.className = "library-summary__value";
    v.textContent = value;
    const l = document.createElement("div");
    l.className = "library-summary__label";
    l.textContent = label;
    stat.append(v, l);
    wrap.append(stat);
  }
  return wrap;
}

function formatBytesCompact(bytes: number): string {
  if (bytes === 0) return "0";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let val = bytes;
  let i = 0;
  while (val >= 1024 && i < units.length - 1) {
    val /= 1024;
    i++;
  }
  return `${val.toFixed(val < 10 ? 1 : 0)} ${units[i]}`;
}

function buildKindPanel(
  catalog: Catalog,
  kind: PackageKind,
  onInstall: (pkg: CatalogPackage) => void,
  onDelete: (pkg: CatalogPackage) => void,
): HTMLElement {
  const panel = document.createElement("div");
  panel.className = "kind-panel";

  // Bundles section (knowledge only — wikipedia/maps don't have bundles yet)
  const bundles = catalog.bundles.filter((b) => b.kind === kind);
  if (bundles.length) {
    const bundleSection = document.createElement("section");
    bundleSection.className = "kind-panel__section";
    const h = document.createElement("h3");
    h.className = "kind-panel__heading";
    h.textContent = "Bundles";
    const sub = document.createElement("p");
    sub.className = "kind-panel__sub";
    sub.textContent = "Curated package collections — installs everything in one click.";
    bundleSection.append(h, sub);

    const bundleGrid = document.createElement("div");
    bundleGrid.className = "bundle-grid";
    for (const bundle of bundles) {
      bundleGrid.append(renderBundleCard(bundle));
    }
    bundleSection.append(bundleGrid);
    panel.append(bundleSection);
  }

  // Group packages by category
  const packages = catalog.packages.filter((p) => p.kind === kind);
  const groups = new Map<string, CatalogPackage[]>();
  for (const pkg of packages) {
    const cat = pkg.category ?? "Other";
    const list = groups.get(cat) ?? [];
    list.push(pkg);
    groups.set(cat, list);
  }

  for (const [category, list] of groups) {
    const cat = document.createElement("section");
    cat.className = "kind-panel__section";
    const h = document.createElement("h3");
    h.className = "kind-panel__heading";
    h.textContent = category;
    cat.append(h);
    const rows = document.createElement("div");
    rows.className = "pkg-list";
    for (const pkg of list) {
      rows.append(PackageRow({ pkg, onInstall, onDelete }));
    }
    cat.append(rows);
    panel.append(cat);
  }

  return panel;
}

function renderBundleCard(bundle: import("../lib/api").CatalogBundle): HTMLElement {
  const card = document.createElement("article");
  card.className = "bundle-card";
  const title = document.createElement("h4");
  title.className = "bundle-card__title";
  title.textContent = bundle.name;
  const desc = document.createElement("p");
  desc.className = "bundle-card__desc";
  desc.textContent = bundle.description;
  const meta = document.createElement("div");
  meta.className = "bundle-card__meta";
  const size = document.createElement("span");
  size.textContent = bundle.size;
  const count = document.createElement("span");
  count.textContent = `${bundle.members.length} packages`;
  meta.append(size, count);
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "btn btn--ghost btn--sm";
  btn.textContent = "Install all";
  btn.disabled = true;
  card.append(title, desc, meta, btn);
  return card;
}
