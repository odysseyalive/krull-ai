/** Tiny toast layer. One container; messages stack and auto-dismiss. */
type ToastKind = "info" | "success" | "error";

let host: HTMLElement | null = null;

function ensureHost(): HTMLElement {
  if (host && document.body.contains(host)) return host;
  host = document.createElement("div");
  host.className = "toast-host";
  host.setAttribute("aria-live", "polite");
  host.setAttribute("aria-atomic", "true");
  document.body.append(host);
  return host;
}

export function toast(message: string, kind: ToastKind = "info", timeoutMs = 4000): void {
  const h = ensureHost();
  const item = document.createElement("div");
  item.className = `toast toast--${kind}`;
  item.setAttribute("role", kind === "error" ? "alert" : "status");
  item.textContent = message;
  h.append(item);
  // animate in
  requestAnimationFrame(() => item.classList.add("toast--visible"));
  window.setTimeout(() => {
    item.classList.remove("toast--visible");
    item.addEventListener("transitionend", () => item.remove(), { once: true });
    // Belt-and-braces removal in case the transition doesn't fire.
    window.setTimeout(() => item.remove(), 600);
  }, timeoutMs);
}
