import { UpdateButton } from "./UpdateButton";
import { HardwarePill } from "./HardwarePill";

/** Persistent top navigation. Sticky, glassy. */
const LINKS: Array<{ href: string; label: string }> = [
  { href: "/", label: "Home" },
  { href: "/library", label: "Library" },
  { href: "/settings", label: "Settings" },
  { href: "/about", label: "About" },
];

export function Nav(currentPath: string): HTMLElement {
  const nav = document.createElement("nav");
  nav.className = "krull-nav";

  const inner = document.createElement("div");
  inner.className = "krull-nav__inner";

  const brand = document.createElement("a");
  brand.className = "krull-nav__brand";
  brand.href = "/";
  const mark = document.createElement("span");
  mark.className = "krull-nav__mark";
  mark.textContent = "✦";
  const label = document.createElement("span");
  label.className = "krull-nav__brand-label";
  label.textContent = "Krull";
  brand.append(mark, label);

  const center = document.createElement("ul");
  center.className = "krull-nav__links";
  for (const link of LINKS) {
    const li = document.createElement("li");
    const a = document.createElement("a");
    a.href = link.href;
    a.textContent = link.label;
    a.className = "krull-nav__link";
    if (link.href === currentPath) a.setAttribute("aria-current", "page");
    li.append(a);
    center.append(li);
  }

  // Right-side actions: hardware status pill (GPU/CPU + free memory)
  // sits to the LEFT of the Update button so the user sees their
  // available memory budget at a glance from anywhere in the app.
  const actions = document.createElement("div");
  actions.className = "krull-nav__actions";
  actions.append(HardwarePill(), UpdateButton());

  inner.append(brand, center, actions);
  nav.append(inner);
  return nav;
}
