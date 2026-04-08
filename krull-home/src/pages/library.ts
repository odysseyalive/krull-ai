import { Header } from "../components/Header";
import { Nav } from "../components/Nav";
import { PackageRow } from "../components/PackageRow";
import { toast } from "../components/Toast";
import {
  deletePackage,
  fetchCatalog,
  fetchDownloadErrors,
  fetchDownloadState,
  startBundleInstall,
  startInstall,
  streamJob,
  type Catalog,
  type CatalogBundle,
  type CatalogPackage,
  type DownloadErrorEntry,
  type DownloadStateSnapshot,
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
  let downloadState: DownloadStateSnapshot = { active: null, queue: [] };
  try {
    // Fetch the catalog and the persistent download state in parallel.
    // The download state tells us whether a previous download is still
    // in progress (possibly on another tab or started from a CLI run)
    // so we can re-hydrate the row's UI and resubscribe its SSE.
    const [fetchedCatalog, fetchedState] = await Promise.all([
      fetchCatalog(),
      fetchDownloadState().catch(() => ({ active: null, queue: [] })),
    ]);
    catalog = fetchedCatalog;
    downloadState = fetchedState;
  } catch (err) {
    status.textContent = `Failed to load catalog: ${(err as Error).message}`;
    return root;
  }
  status.remove();

  // ----- Disk usage strip -----
  const summary = renderSummary(catalog);
  section.append(summary);

  // ----- Cross-tab download banner -----
  // Shown when an active download is happening on a tab OTHER than the
  // currently selected one, so the user knows the queue has something
  // running even when they can't see it in the current panel.
  const banner = document.createElement("div");
  banner.className = "library-banner";
  banner.style.display = "none";
  section.append(banner);

  // Track SSE subscriptions so a single streamJob attaches to each job
  // exactly once, even if the user re-opens the same tab.
  const activeStreams = new Map<string, () => void>();

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
    applyDownloadStateToPanel();
    updateBanner();
  }

  /**
   * Walk the current panel and mark any rows/cards that correspond to
   * the active download or queued entries. Also resubscribes SSE for
   * the active job so the button label continues to tick up. This is
   * called after every panel render (tab switch) and after every state
   * refresh (polling).
   */
  function applyDownloadStateToPanel() {
    const { active, queue } = downloadState;

    // Active download on the current tab → hydrate the row and
    // resubscribe to its SSE stream.
    if (active && active.kind === current) {
      const row = panel.querySelector(
        `.pkg-row[data-key="${cssEscape(active.key)}"][data-kind="${active.kind}"]`,
      ) as HTMLElement | null;
      if (row) {
        row.classList.add("pkg-row--installing");
        const button = row.querySelector("button") as HTMLButtonElement | null;
        if (button) {
          button.disabled = true;
          button.textContent =
            active.percent != null ? `${active.percent}%` : "Downloading…";
        }
        // Resubscribe SSE if we're not already streaming this job.
        if (!activeStreams.has(active.jobId)) {
          jobStarted();
          const stop = streamJob(active.jobId, (ev) => {
            applyJobEvent(ev, row, button);
            if (ev.phase === "done" || ev.phase === "failed") {
              activeStreams.get(active.jobId)?.();
              activeStreams.delete(active.jobId);
              jobEnded();
            }
          });
          activeStreams.set(active.jobId, stop);
        }
      }
      // Active bundle on the current tab.
      const bundleCard = panel.querySelector(
        `.bundle-card[data-key="${cssEscape(active.key)}"]`,
      ) as HTMLElement | null;
      if (bundleCard) {
        bundleCard.classList.add("bundle-card--installing");
        const btn = bundleCard.querySelector("button") as HTMLButtonElement | null;
        if (btn) {
          btn.disabled = true;
          btn.textContent =
            active.percent != null ? `${active.percent}%` : "Downloading…";
        }
      }
    }

    // Queued entries on the current tab → show "Queued (#N)" on the
    // matching rows so the user knows they're already in line.
    queue.forEach((q, idx) => {
      if (q.kind !== current) return;
      const row = panel.querySelector(
        `.pkg-row[data-key="${cssEscape(q.key)}"][data-kind="${q.kind}"]`,
      ) as HTMLElement | null;
      if (row) {
        row.classList.add("pkg-row--installing");
        const button = row.querySelector("button") as HTMLButtonElement | null;
        if (button) {
          button.disabled = true;
          const position = idx + (active ? 2 : 1);
          button.textContent = `Queued (#${position})`;
        }
      }
    });
  }

  /**
   * Show a top-of-section banner when an active download is running on
   * a tab other than the one currently selected. Gives the user a
   * persistent hint that clicking Install on another tab will queue.
   */
  function updateBanner() {
    const { active } = downloadState;
    if (active && active.kind !== current) {
      banner.style.display = "";
      const pct = active.percent != null ? `${active.percent}%` : "downloading…";
      banner.textContent =
        `${capitalize(active.kind)} install in progress: ${active.name} (${pct}). ` +
        `New installs on this tab will be queued.`;
    } else {
      banner.style.display = "none";
      banner.textContent = "";
    }
  }

  /**
   * Poll the persistent download state every 2 seconds while the
   * library page is mounted. This is the recovery mechanism for the
   * case where the user navigated away mid-download: on return, the
   * poll hydrates the row before any SSE events arrive.
   */
  async function refreshDownloadState() {
    try {
      const fresh = await fetchDownloadState();
      downloadState = fresh;
      applyDownloadStateToPanel();
      updateBanner();
    } catch {
      /* ignore — endpoint may be momentarily unavailable */
    }
  }
  const statePollId = window.setInterval(refreshDownloadState, 2000);
  // Clean up polling when the page element is detached from the DOM.
  const disconnectObserver = new MutationObserver(() => {
    if (!document.body.contains(root)) {
      window.clearInterval(statePollId);
      for (const stop of activeStreams.values()) stop();
      activeStreams.clear();
      disconnectObserver.disconnect();
    }
  });
  disconnectObserver.observe(document.body, { childList: true, subtree: true });

  async function handleBundleInstall(bundle: CatalogBundle) {
    const card = panel.querySelector(
      `.bundle-card[data-key="${cssEscape(bundle.key)}"]`,
    ) as HTMLElement | null;
    if (!card) return;
    const button = card.querySelector("button") as HTMLButtonElement | null;
    const phase = card.querySelector(".bundle-card__phase") as HTMLElement | null;
    card.classList.add("bundle-card--installing");
    if (button) {
      button.disabled = true;
      button.textContent = "Queued…";
    }
    if (phase) phase.textContent = "Queued…";
    jobStarted();
    // Seed every non-installed member row as "queued" so users see
    // the whole pipeline lit up the moment they click install, not
    // just the first member that the script reaches. Already-installed
    // rows keep their ✓ state from the initial render.
    card.querySelectorAll<HTMLElement>(
      ".bundle-member-row:not(.bundle-member-row--installed)",
    ).forEach((row) => {
      row.className = row.className
        .split(/\s+/)
        .filter((c) => !c.startsWith("bundle-member-row--"))
        .concat("bundle-member-row--queued")
        .join(" ");
      const icon = row.querySelector(".bundle-member-row__icon") as HTMLElement | null;
      if (icon) icon.textContent = bundleMemberStatusIcon("queued");
      const label = row.querySelector(
        ".bundle-member-row__status",
      ) as HTMLElement | null;
      if (label) label.textContent = bundleMemberStatusLabel("queued", undefined);
    });

    try {
      const { jobId } = await startBundleInstall(bundle.kind, bundle.key);
      const stop = streamJob(jobId, (ev) => {
        // Route per-member events into the details panel. Top-level
        // phase/message/percent updates still drive the summary row.
        updateBundleMemberRow(card, ev);

        if (phase && ev.message) phase.textContent = ev.message;
        if (ev.phase === "queued") {
          if (button) button.textContent = ev.message ?? "Queued";
        }
        if (typeof ev.percent === "number") {
          if (button) button.textContent = `${ev.percent}%`;
        }
        if (ev.phase === "restarting" && button) button.textContent = "Restarting…";
        if (ev.phase === "done") {
          stop();
          if (button) button.textContent = "Installed";
          toast(`Installed bundle ${bundle.name}.`, "success");
          jobEnded();
        } else if (ev.phase === "failed") {
          stop();
          toast(`Bundle install failed: ${ev.error ?? "unknown error"}`, "error", 6000);
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
    row.classList.add("pkg-row--installing");
    if (button) {
      button.disabled = true;
      button.textContent = "Queued…";
    }
    jobStarted();
    try {
      const { jobId } = await startInstall(pkg.kind, pkg.key);
      const stop = streamJob(jobId, (ev) => {
        applyJobEvent(ev, row, button);
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

  // ----- Error log panel (collapsed by default) -----
  const errorSection = document.createElement("details");
  errorSection.className = "library-errors";
  const errorSummary = document.createElement("summary");
  errorSummary.className = "library-errors__summary";
  errorSummary.textContent = "Download errors";
  errorSection.append(errorSummary);
  const errorList = document.createElement("div");
  errorList.className = "library-errors__list";
  errorSection.append(errorList);
  section.append(errorSection);

  async function loadErrors() {
    try {
      const errors = await fetchDownloadErrors(100);
      if (errors.length === 0) {
        errorSummary.textContent = "Download errors (none)";
        errorList.textContent = "No download failures recorded.";
        return;
      }
      errorSummary.textContent = `Download errors (${errors.length})`;
      errorList.replaceChildren(...errors.map(renderErrorEntry));
    } catch (err) {
      errorList.textContent = `Failed to load error log: ${(err as Error).message}`;
    }
  }
  errorSection.addEventListener("toggle", () => {
    if (errorSection.open) void loadErrors();
  });

  return root;
}

function renderErrorEntry(entry: DownloadErrorEntry): HTMLElement {
  const row = document.createElement("div");
  row.className = "library-errors__entry";
  const head = document.createElement("div");
  head.className = "library-errors__head";
  const ts = new Date(entry.timestamp).toLocaleString();
  const status = entry.httpStatus != null ? ` HTTP ${entry.httpStatus}` : "";
  head.textContent = `${ts} — ${entry.kind}/${entry.key}${status}`;
  const url = document.createElement("code");
  url.className = "library-errors__url";
  url.textContent = entry.url || "(no url)";
  const reason = document.createElement("div");
  reason.className = "library-errors__reason";
  reason.textContent = entry.reason;
  row.append(head, url, reason);
  return row;
}

function applyJobEvent(
  ev: JobEvent,
  _row: HTMLElement,
  button: HTMLButtonElement | null,
) {
  if (!button) return;
  switch (ev.phase) {
    case "queued":
      // The queue worker emits queued events with messages like
      // "Up next…" or "Queued (#3)". Show those verbatim so the user
      // knows where they are in line.
      button.textContent = ev.message ?? "Queued";
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

function capitalize(s: string): string {
  return s.length > 0 ? s[0].toUpperCase() + s.slice(1) : s;
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
    // Build a lookup of package records so each bundle card can check
    // not just "installed" but also corrupt/partialBytes for each
    // member — needed to render "Resume (X%)" on a bundle whose
    // members include truncated files.
    const packagesByKey = new Map(
      catalog.packages.map((p) => [p.key, p] as const),
    );
    for (const bundle of bundles) {
      bundleGrid.append(renderBundleCard(bundle, packagesByKey, onBundleInstall));
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
  packagesByKey: Map<string, CatalogPackage>,
  onInstall: (bundle: CatalogBundle) => void,
): HTMLElement {
  const total = bundle.members.length;

  // Walk every member and classify it — this drives both the card
  // summary line and the action button label.
  let installedCount = 0;
  let truncatedCount = 0;
  let unrecoverableCount = 0;
  let partialBytes = 0;
  let expectedBytes = 0;
  for (const key of bundle.members) {
    const pkg = packagesByKey.get(key);
    if (!pkg) continue;
    const exp = parseBundleMemberSize(pkg.size);
    expectedBytes += exp;
    if (pkg.installed) {
      installedCount++;
      partialBytes += pkg.installedSizeBytes ?? exp;
    } else if (pkg.corrupt === "truncated" && pkg.partialBytes) {
      truncatedCount++;
      partialBytes += pkg.partialBytes;
    } else if (pkg.corrupt) {
      unrecoverableCount++;
    }
  }
  const allInstalled = installedCount === total;
  const someInstalled = installedCount > 0 && !allInstalled;
  const hasPartials = truncatedCount > 0;

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
  } else if (someInstalled && hasPartials) {
    count.textContent = `${installedCount} installed, ${truncatedCount} partial, ${total} total`;
  } else if (someInstalled) {
    count.textContent = `${installedCount} of ${total} installed`;
  } else if (hasPartials) {
    count.textContent = `${truncatedCount} partial, ${total} total`;
  } else {
    count.textContent = `${total} packages`;
  }
  meta.append(size, count);

  // Live status line — populated during install
  const phase = document.createElement("p");
  phase.className = "bundle-card__phase";

  // ---- Collapsible per-member details panel ----
  //
  // Each member becomes a row showing its current state so the user can
  // see at a glance which pieces are done, which are partial, which are
  // corrupt, and (once install starts) which is actively downloading.
  // The state here is seeded from the static catalog, then updated live
  // by handleBundleInstall's SSE handler via updateBundleMemberRow().
  const details = document.createElement("details");
  details.className = "bundle-card__details";
  const summary = document.createElement("summary");
  summary.className = "bundle-card__details-summary";
  summary.textContent = `Show ${total} members`;
  details.append(summary);

  const memberList = document.createElement("ul");
  memberList.className = "bundle-member-list";
  for (const key of bundle.members) {
    const pkg = packagesByKey.get(key);
    let initial: BundleMemberStatus;
    if (!pkg) initial = "missing";
    else if (pkg.installed) initial = "installed";
    else if (pkg.corrupt === "truncated" && pkg.partialBytes) initial = "partial";
    else if (pkg.corrupt) initial = "corrupt";
    else initial = "pending";
    memberList.append(renderBundleMemberRow(key, pkg, initial));
  }
  details.append(memberList);

  if (allInstalled) {
    // Already complete — show a badge in place of the action button.
    const badge = document.createElement("span");
    badge.className = "bundle-card__installed-badge";
    badge.textContent = "Installed";
    card.append(title, desc, meta, phase, details, badge);
  } else {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "btn btn--primary btn--sm";

    // Button label reflects the worst-case state of the bundle:
    //   - any partial member   → "Resume (X%)"  + accent styling
    //   - all missing          → "Install all"
    //   - some fully installed → "Install N missing"
    if (hasPartials && expectedBytes > 0) {
      const pct = Math.min(99, Math.round((partialBytes / expectedBytes) * 100));
      btn.textContent = `Resume (${pct}%)`;
      btn.classList.add("btn--resume");
    } else if (someInstalled) {
      btn.textContent = `Install ${total - installedCount} missing`;
    } else {
      btn.textContent = "Install all";
    }
    if (unrecoverableCount > 0) {
      btn.title = `${unrecoverableCount} member file${unrecoverableCount === 1 ? "" : "s"} corrupt and will be re-downloaded from scratch.`;
    }

    btn.addEventListener("click", () => onInstall(bundle));
    card.append(title, desc, meta, phase, details, btn);
  }
  return card;
}

type BundleMemberStatus =
  | "installed"     // ✓ fully on disk and valid
  | "partial"       // ⟳ truncated, resumable
  | "corrupt"       // ✗ corrupt, will be wiped
  | "pending"       // ○ not yet attempted
  | "queued"        // … next in line (set when install starts)
  | "downloading"   // ⏳ active
  | "failed"        // ✗ failed this session
  | "missing";      // ? not in catalog (shouldn't happen)

function bundleMemberStatusIcon(s: BundleMemberStatus): string {
  switch (s) {
    case "installed": return "✓";
    case "partial": return "⟳";
    case "corrupt": return "✗";
    case "pending": return "○";
    case "queued": return "…";
    case "downloading": return "⏳";
    case "failed": return "✗";
    case "missing": return "?";
  }
}

function bundleMemberStatusLabel(
  s: BundleMemberStatus,
  pkg: CatalogPackage | undefined,
): string {
  switch (s) {
    case "installed": return "Installed";
    case "partial": {
      // Show a resume percent if we can compute one.
      const exp = parseBundleMemberSize(pkg?.size);
      if (pkg?.partialBytes && exp > 0) {
        const pct = Math.min(99, Math.round((pkg.partialBytes / exp) * 100));
        return `Resume (${pct}%)`;
      }
      return "Partial";
    }
    case "corrupt": return `Corrupt (${pkg?.corrupt ?? "unknown"})`;
    case "pending": return "Not installed";
    case "queued": return "Queued";
    case "downloading": return "Downloading";
    case "failed": return "Failed";
    case "missing": return "Missing from catalog";
  }
}

function renderBundleMemberRow(
  key: string,
  pkg: CatalogPackage | undefined,
  status: BundleMemberStatus,
): HTMLLIElement {
  const li = document.createElement("li");
  li.className = `bundle-member-row bundle-member-row--${status}`;
  li.dataset.memberKey = key;

  const icon = document.createElement("span");
  icon.className = "bundle-member-row__icon";
  icon.textContent = bundleMemberStatusIcon(status);

  const name = document.createElement("span");
  name.className = "bundle-member-row__name";
  // Strip the common "Brand — " prefix from the display name. Inside a
  // bundle card whose title is already "Gutenberg — All English",
  // seeing "Gutenberg — Fiction" / "Gutenberg — Philosophy" wastes
  // every row's first 12 characters on the same redundant prefix.
  // Full name is retained in the tooltip so nothing is lost.
  const fullName = pkg?.name ?? key;
  const shortName = fullName.includes(" — ")
    ? fullName.slice(fullName.indexOf(" — ") + 3)
    : fullName;
  name.textContent = shortName;
  name.title = `${fullName}${pkg?.size ? ` — ${pkg.size}` : ""}`;

  const label = document.createElement("span");
  label.className = "bundle-member-row__status";
  label.textContent = bundleMemberStatusLabel(status, pkg);

  // Thin progress bar used while this member is `downloading`. We set
  // width=0 by default; handleBundleInstall updates it via CSS var.
  const bar = document.createElement("span");
  bar.className = "bundle-member-row__bar";
  const barFill = document.createElement("span");
  barFill.className = "bundle-member-row__bar-fill";
  bar.append(barFill);

  li.append(icon, name, label, bar);
  return li;
}

/**
 * Apply a streaming JobEvent to a bundle card's per-member rows. Called
 * from handleBundleInstall whenever an event arrives for the active
 * bundle. Idempotent: running out-of-order events won't corrupt state,
 * because events carry both `memberKey` and `memberStatus` and we only
 * mutate the row they target.
 *
 * Side effect: if memberStatus is "downloading", we also update the
 * per-row progress bar from ev.percent.
 */
function updateBundleMemberRow(card: HTMLElement, ev: JobEvent): void {
  if (!ev.memberKey) return;
  const row = card.querySelector(
    `.bundle-member-row[data-member-key="${cssEscape(ev.memberKey)}"]`,
  ) as HTMLElement | null;
  if (!row) return;

  const newStatus: BundleMemberStatus | null =
    ev.memberStatus === "installed" ? "installed" :
    ev.memberStatus === "downloading" ? "downloading" :
    ev.memberStatus === "failed" ? "failed" :
    null;
  if (!newStatus) return;

  // Strip any previous status class and re-apply the new one.
  row.className = row.className
    .split(/\s+/)
    .filter((c) => !c.startsWith("bundle-member-row--"))
    .concat(`bundle-member-row--${newStatus}`)
    .join(" ");

  const icon = row.querySelector(".bundle-member-row__icon") as HTMLElement | null;
  if (icon) icon.textContent = bundleMemberStatusIcon(newStatus);

  const label = row.querySelector(".bundle-member-row__status") as HTMLElement | null;
  if (label) label.textContent = bundleMemberStatusLabel(newStatus, undefined);

  const barFill = row.querySelector(
    ".bundle-member-row__bar-fill",
  ) as HTMLElement | null;
  if (barFill) {
    if (newStatus === "downloading" && typeof ev.percent === "number") {
      barFill.style.width = `${Math.max(0, Math.min(100, ev.percent))}%`;
    } else if (newStatus === "installed") {
      barFill.style.width = "100%";
    } else if (newStatus === "failed") {
      barFill.style.width = "0%";
    }
  }
}

/** parseBundleMemberSize mirrors dl_parse_size / parseSizeBytes. */
function parseBundleMemberSize(s: string | undefined): number {
  if (!s) return 0;
  const m = s.match(/^\s*~?\s*([\d.]+)\s*([KMGT]?)B?/i);
  if (!m) return 0;
  const v = parseFloat(m[1]);
  const u = (m[2] || "").toUpperCase();
  const mult =
    u === "T" ? 1024 ** 4 :
    u === "G" ? 1024 ** 3 :
    u === "M" ? 1024 ** 2 :
    u === "K" ? 1024 : 1;
  return Math.round(v * mult);
}
