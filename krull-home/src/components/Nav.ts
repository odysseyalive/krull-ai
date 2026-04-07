import { UpdateButton } from "./UpdateButton";

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

  // Right-side actions: update button + (room for future status indicators).
  const actions = document.createElement("div");
  actions.className = "krull-nav__actions";
  actions.append(UpdateButton());

  inner.append(brand, center, actions);
  nav.append(inner);
  return nav;
}
