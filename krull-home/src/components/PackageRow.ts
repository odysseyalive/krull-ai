import { formatBytes, type CatalogPackage } from "../lib/api";

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
  if (pkg.installed && pkg.installedSizeBytes != null) {
    sizeLine.textContent = formatBytes(pkg.installedSizeBytes);
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
    const install = document.createElement("button");
    install.type = "button";
    install.className = "btn btn--primary btn--sm";
    install.textContent = "Install";
    install.disabled = !!disabled;
    if (!disabled && opts.onInstall) {
      install.addEventListener("click", () => opts.onInstall!(pkg));
    }
    actions.append(install);
  }

  meta.append(actions);

  // Full-width progress strip — hidden until install starts. Spans both
  // grid columns so it sits cleanly below the row content.
  const progress = document.createElement("div");
  progress.className = "krull-progress pkg-row__progress";
  const fill = document.createElement("div");
  fill.className = "krull-progress__fill";
  const label = document.createElement("div");
  label.className = "krull-progress__label";
  label.textContent = "0%";
  progress.append(fill, label);

  row.append(info, meta, progress);
  return row;
}
