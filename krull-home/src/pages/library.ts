import { Header } from "../components/Header";
import { Nav } from "../components/Nav";
import { PackageRow } from "../components/PackageRow";
import { toast } from "../components/Toast";
import {
  deletePackage,
  fetchCatalog,
  startBundleInstall,
  startInstall,
  streamJob,
  type Catalog,
  type CatalogBundle,
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

  // Active-job counter — refreshCatalog only fires when this drops to
  // zero. Without this, the first finished job would re-render the
  // panel and tear down every other queued row mid-flight.
  let activeJobs = 0;
  function jobStarted() {
    activeJobs++;
  }
  function jobEnded() {
    activeJobs = Math.max(0, activeJobs - 1);
    if (activeJobs === 0) {
      void refreshCatalog();
    }
  }

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
    panel.replaceChildren(
      buildKindPanel(catalog, current, handleInstall, handleDelete, handleBundleInstall),
    );
  }

  async function handleBundleInstall(bundle: CatalogBundle) {
    const card = panel.querySelector(
      `.bundle-card[data-key="${cssEscape(bundle.key)}"]`,
    ) as HTMLElement | null;
    if (!card) return;
    const button = card.querySelector("button") as HTMLButtonElement | null;
    const phase = card.querySelector(".bundle-card__phase") as HTMLElement | null;
    const progress = card.querySelector(".krull-progress") as HTMLElement | null;
    const fill = card.querySelector(".krull-progress__fill") as HTMLElement | null;
    const pLabel = card.querySelector(".krull-progress__label") as HTMLElement | null;
    card.classList.add("bundle-card--installing");
    if (button) {
      button.disabled = true;
      button.textContent = "Queued…";
    }
    if (phase) phase.textContent = "Queued…";
    jobStarted();
    try {
      const { jobId } = await startBundleInstall(bundle.kind, bundle.key);
      const stop = streamJob(jobId, (ev) => {
        if (phase && ev.message) phase.textContent = ev.message;
        if (ev.phase === "queued") {
          if (button) button.textContent = ev.message ?? "Queued";
        }
        if (typeof ev.percent === "number") {
          if (fill) fill.style.width = `${ev.percent}%`;
          if (pLabel) pLabel.textContent = `${ev.percent}%`;
          if (button) button.textContent = `${ev.percent}%`;
        }
        if (ev.phase === "restarting" && pLabel) pLabel.textContent = "Restarting…";
        if (ev.phase === "done") {
          stop();
          if (button) button.textContent = "Installed";
          toast(`Installed bundle ${bundle.name}.`, "success");
          jobEnded();
        } else if (ev.phase === "failed") {
          stop();
          toast(`Bundle install failed: ${ev.error ?? "unknown error"}`, "error", 6000);
          if (progress) void progress;
          jobEnded();
        }
      });
    } catch (err) {
      toast((err as Error).message, "error", 6000);
      jobEnded();
    }
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
      button.textContent = "Queued…";
    }
    jobStarted();
    try {
      const { jobId } = await startInstall(pkg.kind, pkg.key);
      const stop = streamJob(jobId, (ev) => {
        applyJobEvent(ev, row, button, bar);
        if (ev.phase === "done") {
          stop();
          toast(`Installed ${pkg.name}.`, "success");
          jobEnded();
        } else if (ev.phase === "failed") {
          stop();
          toast(`Install failed: ${ev.error ?? "unknown error"}`, "error", 6000);
          row.classList.remove("pkg-row--installing");
          if (button) {
            button.disabled = false;
            button.textContent = "Install";
          }
          jobEnded();
        }
      });
    } catch (err) {
      toast((err as Error).message, "error", 6000);
      row.classList.remove("pkg-row--installing");
      if (button) {
        button.disabled = false;
        button.textContent = "Install";
      }
      jobEnded();
    }
  }

  async function handleDelete(pkg: CatalogPackage) {
    if (!confirmInline(pkg)) return;
    const row = panel.querySelector(
      `.pkg-row[data-key="${cssEscape(pkg.key)}"][data-kind="${pkg.kind}"]`,
    ) as HTMLElement | null;
    const button = row?.querySelector("button") as HTMLButtonElement | null;
    if (button) {
      button.disabled = true;
      button.textContent = "Queued…";
    }
    jobStarted();
    try {
      const { jobId } = await deletePackage(pkg.kind, pkg.key);
      const stop = streamJob(jobId, (ev) => {
        if (button) {
          if (ev.phase === "queued") button.textContent = ev.message ?? "Queued";
          else if (ev.phase === "downloading") button.textContent = "Deleting…";
          else if (ev.phase === "restarting") button.textContent = "Restarting…";
        }
        if (ev.phase === "done") {
          stop();
          toast(`Deleted ${pkg.name}.`, "success");
          jobEnded();
        } else if (ev.phase === "failed") {
          stop();
          toast(`Delete failed: ${ev.error ?? "unknown error"}`, "error", 6000);
          if (button) {
            button.disabled = false;
            button.textContent = "Delete";
          }
          jobEnded();
        }
      });
    } catch (err) {
      toast(`Delete failed: ${(err as Error).message}`, "error", 6000);
      if (button) {
        button.disabled = false;
        button.textContent = "Delete";
      }
      jobEnded();
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
  // Update both the colored fill and the percent label.
  const fill = row.querySelector(".krull-progress__fill") as HTMLElement | null;
  const label = row.querySelector(".krull-progress__label") as HTMLElement | null;
  if (typeof ev.percent === "number") {
    if (fill) fill.style.width = `${ev.percent}%`;
    if (label) label.textContent = `${ev.percent}%`;
  }
  // Older code paths used a `bar` arg directly — keep it in sync too.
  if (bar && typeof ev.percent === "number") {
    bar.style.width = `${ev.percent}%`;
  }
  if (!button) return;
  switch (ev.phase) {
    case "queued":
      // The queue worker emits queued events with messages like
      // "Up next…" or "Queued (#3)". Show those verbatim so the user
      // knows where they are in line.
      button.textContent = ev.message ?? "Queued";
      if (label) label.textContent = ev.message ?? "Queued";
      if (fill) fill.style.width = "0%";
      break;
    case "downloading":
      button.textContent = ev.percent != null ? `${ev.percent}%` : "Downloading";
      break;
    case "restarting":
      button.textContent = "Restarting";
      if (fill) fill.style.width = "100%";
      if (label) label.textContent = "Restarting…";
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
  onBundleInstall: (bundle: CatalogBundle) => void,
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
    // Build a quick lookup of installed package keys so each bundle
    // card can compute "X of N members installed" without scanning
    // the whole package list per render.
    const installedKeys = new Set(
      catalog.packages.filter((p) => p.installed).map((p) => p.key),
    );
    for (const bundle of bundles) {
      bundleGrid.append(renderBundleCard(bundle, installedKeys, onBundleInstall));
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

function renderBundleCard(
  bundle: CatalogBundle,
  installedKeys: Set<string>,
  onInstall: (bundle: CatalogBundle) => void,
): HTMLElement {
  const total = bundle.members.length;
  const installedCount = bundle.members.reduce(
    (acc, k) => acc + (installedKeys.has(k) ? 1 : 0),
    0,
  );
  const allInstalled = installedCount === total;
  const someInstalled = installedCount > 0 && !allInstalled;

  const card = document.createElement("article");
  card.className = `bundle-card${allInstalled ? " bundle-card--installed" : ""}`;
  card.dataset.key = bundle.key;

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
  if (allInstalled) {
    count.textContent = `${total} packages`;
  } else if (someInstalled) {
    count.textContent = `${installedCount} of ${total} installed`;
  } else {
    count.textContent = `${total} packages`;
  }
  meta.append(size, count);

  // Live status line — populated during install
  const phase = document.createElement("p");
  phase.className = "bundle-card__phase";

  if (allInstalled) {
    // Already complete — show a badge in place of the action button.
    const badge = document.createElement("span");
    badge.className = "bundle-card__installed-badge";
    badge.textContent = "Installed";
    card.append(title, desc, meta, phase, badge);
  } else {
    // Progress strip lives between the phase line and the button so
    // it's visually attached to the action when it appears.
    const progress = document.createElement("div");
    progress.className = "krull-progress bundle-card__progress";
    const fill = document.createElement("div");
    fill.className = "krull-progress__fill";
    const pLabel = document.createElement("div");
    pLabel.className = "krull-progress__label";
    pLabel.textContent = "0%";
    progress.append(fill, pLabel);

    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "btn btn--primary btn--sm";
    btn.textContent = someInstalled
      ? `Install ${total - installedCount} missing`
      : "Install all";
    btn.addEventListener("click", () => onInstall(bundle));
    card.append(title, desc, meta, phase, progress, btn);
  }
  return card;
}
