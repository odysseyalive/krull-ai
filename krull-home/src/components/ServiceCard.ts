import type { ServiceStatus } from "../lib/api";

interface ServiceCardOptions {
  status: ServiceStatus;
  description: string;
  glyph: string; // single decorative character
}

const STATE_LABEL: Record<ServiceStatus["state"], string> = {
  running: "Online",
  exited: "Stopped",
  restarting: "Restarting",
  missing: "Not installed",
  unknown: "Unknown",
};

export function ServiceCard(opts: ServiceCardOptions): HTMLElement {
  const { status, description, glyph } = opts;
  const card = document.createElement("a");
  card.className = `service-card service-card--${status.state}`;
  card.href = status.url;
  card.target = "_blank";
  card.rel = "noopener noreferrer";

  const ornament = document.createElement("div");
  ornament.className = "service-card__ornament";
  ornament.setAttribute("aria-hidden", "true");
  ornament.textContent = glyph;

  const body = document.createElement("div");
  body.className = "service-card__body";

  const title = document.createElement("h2");
  title.className = "service-card__title";
  title.textContent = status.name;

  const desc = document.createElement("p");
  desc.className = "service-card__desc";
  desc.textContent = description;

  const meta = document.createElement("div");
  meta.className = "service-card__meta";

  const dot = document.createElement("span");
  dot.className = "service-card__dot";
  dot.setAttribute("aria-hidden", "true");

  const stateLabel = document.createElement("span");
  stateLabel.className = "service-card__state";
  stateLabel.textContent = STATE_LABEL[status.state];

  const url = document.createElement("span");
  url.className = "service-card__url";
  url.textContent = status.url.replace(/^https?:\/\//, "");

  meta.append(dot, stateLabel, url);
  body.append(title, desc, meta);

  const arrow = document.createElement("span");
  arrow.className = "service-card__arrow";
  arrow.setAttribute("aria-hidden", "true");
  arrow.textContent = "↗";

  card.append(ornament, body, arrow);
  return card;
}
