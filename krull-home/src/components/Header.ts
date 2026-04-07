/**
 * Painted page header. The hero painting fills the band; a soft gradient
 * masks the bottom edge so it dissolves into the page background. Title
 * and optional eyebrow are layered over the painting in Cormorant Garamond.
 */
export interface HeaderOptions {
  image: string;          // /images/headers/foo.webp
  eyebrow?: string;       // small uppercase chip above the title
  title: string;          // display title
  subtitle?: string;      // one-line lede under the title
}

export function Header(opts: HeaderOptions): HTMLElement {
  const wrap = document.createElement("header");
  wrap.className = "krull-header";
  wrap.style.setProperty("--header-image", `url("${opts.image}")`);

  const overlay = document.createElement("div");
  overlay.className = "krull-header__overlay";

  const inner = document.createElement("div");
  inner.className = "krull-header__inner";

  if (opts.eyebrow) {
    const e = document.createElement("p");
    e.className = "krull-header__eyebrow";
    e.textContent = opts.eyebrow;
    inner.append(e);
  }

  const h1 = document.createElement("h1");
  h1.className = "krull-header__title";
  h1.textContent = opts.title;
  inner.append(h1);

  if (opts.subtitle) {
    const s = document.createElement("p");
    s.className = "krull-header__subtitle";
    s.textContent = opts.subtitle;
    inner.append(s);
  }

  overlay.append(inner);
  wrap.append(overlay);
  return wrap;
}
