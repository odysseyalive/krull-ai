import {
  fetchHardware,
  type Hardware,
  type InferenceConfig,
  type LoadedModel,
} from "../lib/api";

/**
 * Hardware summary strip rendered at the top of /settings.
 *
 * Shows the user what Krull detected so the gray "won't fit" cards
 * in the model picker make sense. Also anchors the hardware-aware
 * recommendations: "on your 16 GB GPU, 9B + 98k context fits with
 * 1.5 GB headroom" only works if the user can see the 16 GB in
 * the first place.
 *
 * Three lines:
 *   1. Detected hardware  (RAM + GPU)
 *   2. Inference config   (Flash Attention + KV cache quant)
 *   3. Currently loaded model + its GPU/CPU split — hides if idle
 */

function formatBytes(bytes: number): string {
  if (bytes >= 1024 ** 3) return `${(bytes / 1024 ** 3).toFixed(1)} GB`;
  if (bytes >= 1024 ** 2) return `${(bytes / 1024 ** 2).toFixed(0)} MB`;
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${bytes} B`;
}

function renderHardwareLine(hw: Hardware): string {
  const parts: string[] = [];
  if (hw.ram.totalBytes > 0) {
    parts.push(`${formatBytes(hw.ram.totalBytes)} RAM`);
  }
  if (hw.gpu.vendor === "nvidia" && typeof hw.gpu.totalBytes === "number") {
    const name = hw.gpu.name ?? "NVIDIA GPU";
    parts.push(`${name} (${formatBytes(hw.gpu.totalBytes)} VRAM)`);
  } else {
    parts.push("CPU only");
  }
  return parts.join(" · ");
}

function renderInferenceLine(cfg: InferenceConfig): string {
  const parts: string[] = [];
  parts.push(cfg.flashAttention ? "Flash Attention on" : "Flash Attention off");
  parts.push(`K/V cache ${cfg.kvCacheType}`);
  return parts.join(" · ");
}

function renderLoadedLine(m: LoadedModel): string {
  const split =
    m.gpuPercent === 100
      ? "100% GPU"
      : m.gpuPercent === 0
        ? "100% CPU"
        : `${m.gpuPercent}% GPU / ${100 - m.gpuPercent}% CPU`;
  const ctxStr = m.contextLength
    ? ` · ${Math.round(m.contextLength / 1024)}k ctx`
    : "";
  return `${m.name} · ${split} · ${formatBytes(m.sizeBytes)}${ctxStr}`;
}

export function HardwareStrip(): HTMLElement {
  const wrap = document.createElement("section");
  wrap.className = "hardware-strip";

  const row1 = document.createElement("div");
  row1.className = "hardware-strip__row";
  const icon1 = document.createElement("span");
  icon1.className = "hardware-strip__icon";
  icon1.textContent = "▣";
  const text1 = document.createElement("span");
  text1.className = "hardware-strip__text";
  text1.textContent = "Detecting hardware…";
  row1.append(icon1, text1);

  const row2 = document.createElement("div");
  row2.className = "hardware-strip__row hardware-strip__row--dim";
  const icon2 = document.createElement("span");
  icon2.className = "hardware-strip__icon";
  icon2.textContent = "⚙";
  const text2 = document.createElement("span");
  text2.className = "hardware-strip__text";
  row2.append(icon2, text2);

  const row3 = document.createElement("div");
  row3.className = "hardware-strip__row hardware-strip__row--loaded";
  row3.style.display = "none";
  const icon3 = document.createElement("span");
  icon3.className = "hardware-strip__icon";
  icon3.textContent = "◉";
  const text3 = document.createElement("span");
  text3.className = "hardware-strip__text";
  row3.append(icon3, text3);

  wrap.append(row1, row2, row3);

  void (async () => {
    try {
      const { hardware, inference, loaded } = await fetchHardware();
      text1.textContent = renderHardwareLine(hardware);
      text2.textContent = renderInferenceLine(inference);
      if (loaded.length > 0) {
        row3.style.display = "";
        text3.textContent = renderLoadedLine(loaded[0]);
      } else {
        row3.style.display = "none";
      }
    } catch (err) {
      text1.textContent = `Hardware detection unavailable (${(err as Error).message})`;
      row2.style.display = "none";
    }
  })();

  return wrap;
}
