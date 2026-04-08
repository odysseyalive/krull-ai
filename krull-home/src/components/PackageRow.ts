import { formatBytes, type CatalogPackage } from "../lib/api";

/**
 * Parse a human-readable size string like "20 GB" / "~65 MB" into bytes.
 * Duplicated (in JS) from scripts/lib/download-log.sh's dl_parse_size so
 * PackageRow can compute a Resume percentage without an extra API call.
 */
function parseSizeBytes(s: string | undefined): number {
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

export interface PackageRowOptions {
  pkg: CatalogPackage;
  onInstall?: (pkg: CatalogPackage) => void;
  onDelete?: (pkg: CatalogPackage) => void;
  /** Phase 5: render with disabled buttons (display-only) */
  disabled?: boolean;
}

export function PackageRow(opts: PackageRowOptions): HTMLElement {
  const { pkg, disabled } = opts;
  const row = document.createElement("article");
  row.className = `pkg-row${pkg.installed ? " pkg-row--installed" : ""}`;
  row.dataset.key = pkg.key;
  row.dataset.kind = pkg.kind;

  const info = document.createElement("div");
  info.className = "pkg-row__info";

  const titleRow = document.createElement("div");
  titleRow.className = "pkg-row__title-row";
  const title = document.createElement("h3");
  title.className = "pkg-row__title";
  title.textContent = pkg.name;
  const key = document.createElement("code");
  key.className = "pkg-row__key";
  key.textContent = pkg.key;
  titleRow.append(title, key);

  const desc = document.createElement("p");
  desc.className = "pkg-row__desc";
  desc.textContent = pkg.description;

  info.append(titleRow, desc);

  // Right-side metadata + actions column
  const meta = document.createElement("div");
  meta.className = "pkg-row__meta";

  const sizeLine = document.createElement("div");
  sizeLine.className = "pkg-row__size";
  // For maps the single-file on-disk size is misleading — a map region
  // install actually downloads the base .pmtiles plus global terrain
  // plus auto-detected nautical and aero charts, distributed across
  // many files. Always show the computed catalog estimate for maps so
  // the user sees the real total. For knowledge/wikipedia packages
  // (single ZIM file) the on-disk size is accurate and preferred.
  if (pkg.kind !== "maps" && pkg.installed && pkg.installedSizeBytes != null) {
    sizeLine.textContent = formatBytes(pkg.installedSizeBytes);
  } else if (pkg.corrupt === "truncated" && pkg.partialBytes && pkg.size) {
    // Show both the partial-on-disk and the catalog target so the user
    // understands why the button says "Resume" and how much is left.
    sizeLine.textContent = `${formatBytes(pkg.partialBytes)} / ${pkg.size}`;
  } else if (pkg.size) {
    sizeLine.textContent = pkg.size;
  }
  meta.append(sizeLine);

  const actions = document.createElement("div");
  actions.className = "pkg-row__actions";

  if (pkg.installed) {
    const badge = document.createElement("span");
    badge.className = "pkg-row__badge";
    badge.textContent = "Installed";
    actions.append(badge);
    const del = document.createElement("button");
    del.type = "button";
    del.className = "btn btn--ghost btn--sm";
    del.textContent = "Delete";
    del.disabled = !!disabled;
    if (!disabled && opts.onDelete) {
      del.addEventListener("click", () => opts.onDelete!(pkg));
    }
    actions.append(del);
  } else {
    // A file may exist on disk but be corrupt. Branch the button
    // label on pkg.corrupt so the user knows what will happen:
    //   "truncated"           → Resume (X%)  — curl -C - will continue
    //   "html" / "magic" / … → Redownload   — installer will wipe + refetch
    //   unset                 → Install     — fresh download
    const install = document.createElement("button");
    install.type = "button";
    install.className = "btn btn--primary btn--sm";

    if (pkg.corrupt === "truncated" && pkg.partialBytes) {
      const total = parseSizeBytes(pkg.size);
      if (total > 0) {
        const pct = Math.min(99, Math.round((pkg.partialBytes / total) * 100));
        install.textContent = `Resume (${pct}%)`;
      } else {
        install.textContent = `Resume (${formatBytes(pkg.partialBytes)})`;
      }
      install.classList.add("btn--resume");
    } else if (pkg.corrupt) {
      install.textContent = "Redownload";
      install.classList.add("btn--redownload");
      install.title = `Previous file was corrupt (${pkg.corrupt}); will be deleted and re-fetched.`;
    } else {
      install.textContent = "Install";
    }

    install.disabled = !!disabled;
    if (!disabled && opts.onInstall) {
      install.addEventListener("click", () => opts.onInstall!(pkg));
    }
    actions.append(install);
  }

  meta.append(actions);

  // Progress is conveyed entirely through the button's text label —
  // the old colored progress bar was redundant and visually noisy.
  row.append(info, meta);
  return row;
}
