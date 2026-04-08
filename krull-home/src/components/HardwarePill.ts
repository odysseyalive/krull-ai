import { fetchHardware, type Hardware } from "../lib/api";

/**
 * Compact hardware indicator that lives in the top nav beside the
 * Update button. Tells the user at a glance whether inference will
 * run on GPU or CPU, and how much memory they have left to play
 * with — so they can spot OOM risk before it bites them.
 *
 * Visual:
 *   [GPU gauge] 12.4 / 16.0 GB
 *   [CPU gauge] 22.1 / 31.0 GB
 *
 * The gauge is a tiny SVG ring that fills counterclockwise as
 * memory gets used, colored amber when healthy, shifting toward
 * red as free memory drops below 20% of total.
 */

function formatGb(bytes: number): string {
  return (bytes / 1024 ** 3).toFixed(1);
}

interface GaugeInput {
  used: number;
  total: number;
}

function gaugeColor(freeFraction: number): string {
  // Healthy: amber. Warning (<30% free): soft red. Critical (<15% free): danger.
  if (freeFraction < 0.15) return "var(--danger)";
  if (freeFraction < 0.3) return "#e28c5c";
  return "var(--warn)";
}

function renderGauge({ used, total }: GaugeInput): SVGElement {
  const svgNs = "http://www.w3.org/2000/svg";
  const size = 20;
  const stroke = 3;
  const r = (size - stroke) / 2;
  const c = 2 * Math.PI * r;
  const usedFraction = total > 0 ? Math.min(1, Math.max(0, used / total)) : 0;
  const freeFraction = 1 - usedFraction;
  const dash = usedFraction * c;
  const color = gaugeColor(freeFraction);

  const svg = document.createElementNS(svgNs, "svg");
  svg.setAttribute("width", String(size));
  svg.setAttribute("height", String(size));
  svg.setAttribute("viewBox", `0 0 ${size} ${size}`);
  svg.setAttribute("class", "hardware-pill__gauge");

  const track = document.createElementNS(svgNs, "circle");
  track.setAttribute("cx", String(size / 2));
  track.setAttribute("cy", String(size / 2));
  track.setAttribute("r", String(r));
  track.setAttribute("fill", "none");
  track.setAttribute("stroke", "rgba(255,255,255,0.08)");
  track.setAttribute("stroke-width", String(stroke));

  const fill = document.createElementNS(svgNs, "circle");
  fill.setAttribute("cx", String(size / 2));
  fill.setAttribute("cy", String(size / 2));
  fill.setAttribute("r", String(r));
  fill.setAttribute("fill", "none");
  fill.setAttribute("stroke", color);
  fill.setAttribute("stroke-width", String(stroke));
  fill.setAttribute("stroke-linecap", "round");
  fill.setAttribute("stroke-dasharray", `${dash} ${c}`);
  // Start the stroke at 12 o'clock and run clockwise as usage grows.
  fill.setAttribute(
    "transform",
    `rotate(-90 ${size / 2} ${size / 2})`,
  );

  svg.append(track, fill);
  return svg;
}

export function HardwarePill(): HTMLElement {
  const el = document.createElement("div");
  el.className = "hardware-pill";
  el.setAttribute("role", "status");
  el.setAttribute("aria-label", "Hardware status");

  const label = document.createElement("span");
  label.className = "hardware-pill__label";
  label.textContent = "…";

  const detail = document.createElement("span");
  detail.className = "hardware-pill__detail";
  detail.textContent = "";

  // Placeholder ring that will be replaced on first fetch.
  const gaugeSlot = document.createElement("span");
  gaugeSlot.className = "hardware-pill__gauge-slot";
  gaugeSlot.append(renderGauge({ used: 0, total: 1 }));

  el.append(gaugeSlot, label, detail);

  void (async () => {
    try {
      const { hardware } = await fetchHardware();
      paint(hardware);
    } catch {
      label.textContent = "—";
      detail.textContent = "detection failed";
      el.classList.add("hardware-pill--err");
    }
  })();

  function paint(hw: Hardware) {
    if (
      hw.gpu.vendor === "nvidia" &&
      typeof hw.gpu.totalBytes === "number" &&
      typeof hw.gpu.freeBytes === "number"
    ) {
      const total = hw.gpu.totalBytes;
      const free = hw.gpu.freeBytes;
      const used = total - free;
      el.classList.add("hardware-pill--gpu");
      el.title = `${hw.gpu.name ?? "GPU"} — ${formatGb(free)} GB free of ${formatGb(total)} GB VRAM`;
      label.textContent = "GPU";
      detail.textContent = `${formatGb(free)} GB free`;
      gaugeSlot.replaceChildren(renderGauge({ used, total }));
      return;
    }
    // CPU path
    el.classList.add("hardware-pill--cpu");
    const total = hw.ram.totalBytes;
    const avail = hw.ram.availableBytes;
    const used = total - avail;
    el.title = `CPU inference — ${formatGb(avail)} GB available of ${formatGb(total)} GB RAM`;
    label.textContent = "CPU";
    detail.textContent = `${formatGb(avail)} GB free`;
    gaugeSlot.replaceChildren(renderGauge({ used, total }));
  }

  return el;
}
