/**
 * Recommended models + Ollama state.
 *
 * The three frob/qwen3.5-instruct variants are the canonical Krull
 * recommendations: same architecture, same tool-calling behavior, three
 * size points. Anything else the user wants is supported via the .env
 * OLLAMA_MODEL field directly — this list is purely for the picker UX.
 */
/* Ollama HTTP endpoint inside the docker network. The container hostname
 * matches the compose service name. We hit /api/tags rather than shelling
 * out to `ollama list` so we get clean JSON instead of TTY-mangled text. */
const OLLAMA_URL = process.env.OLLAMA_URL ?? "http://krull-ollama:11434";

export interface RecommendedModel {
  key: string;
  label: string;
  vram: string;
  description: string;
  bestFor: string;
}

export const RECOMMENDED_MODELS: RecommendedModel[] = [
  {
    key: "frob/qwen3.5-instruct:4b",
    label: "Qwen 3.5 Instruct · 4B",
    vram: "~3 GB",
    description: "Smallest variant. Quick responses on lighter hardware.",
    bestFor: "Laptops, integrated GPUs, quick prototyping.",
  },
  {
    key: "frob/qwen3.5-instruct:9b",
    label: "Qwen 3.5 Instruct · 9B",
    vram: "~6 GB",
    description: "The recommended default. Strong coding + reliable Anthropic-style tool calling.",
    bestFor: "Most users. Daily Claude Code work on a 6–12 GB GPU.",
  },
  {
    key: "frob/qwen3.5-instruct:27b",
    label: "Qwen 3.5 Instruct · 27B",
    vram: "~16 GB",
    description: "Best quality variant. Slower responses, sharper reasoning.",
    bestFor: "16+ GB GPUs (RTX 4080/4090, A6000, 7900 XTX).",
  },
];

interface OllamaTagsResponse {
  models?: Array<{ name?: string }>;
}

/**
 * List locally-pulled Ollama models via Ollama's own HTTP API.
 * Returns model names (e.g. "frob/qwen3.5-instruct:9b").
 * Returns [] if Ollama is unreachable — the picker degrades to "no
 * models installed" rather than throwing.
 */
export async function listInstalledModels(): Promise<string[]> {
  try {
    const ctrl = new AbortController();
    const timeout = setTimeout(() => ctrl.abort(), 4000);
    const res = await fetch(`${OLLAMA_URL}/api/tags`, { signal: ctrl.signal });
    clearTimeout(timeout);
    if (!res.ok) return [];
    const data = (await res.json()) as OllamaTagsResponse;
    return (data.models ?? [])
      .map((m) => m.name)
      .filter((name): name is string => typeof name === "string" && name.length > 0);
  } catch {
    return [];
  }
}
