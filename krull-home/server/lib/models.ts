/**
 * Recommended models + Ollama state.
 *
 * Two brains, both empirically verified end-to-end through the SSE proxy
 * (BM25 passage retrieval + grounded-answer pass) on the same fixture:
 * `/study-prep translate My name is Francis. How are you doing?`. Both
 * produce the Opus-reference output `Francis nayka yax̣al. qʰata mayka?`
 * across two consecutive runs. Anything else the user wants is supported
 * via the .env OLLAMA_MODEL field directly — this list is purely for the
 * picker UX.
 */
/* Ollama HTTP endpoint inside the docker network. The container hostname
 * matches the compose service name. We hit /api/tags rather than shelling
 * out to `ollama list` so we get clean JSON instead of TTY-mangled text. */
const OLLAMA_URL = process.env.OLLAMA_URL ?? "http://krull-ollama:11434";

/**
 * Optional per-model recommendation for OLLAMA_NUM_CTX and CONTEXT_COMPACT_LIMIT.
 * Surfaced on the picker card so the user can see the model's "natural" context
 * budget alongside the VRAM tier. Not auto-applied — env settings still come
 * from the env editor; this is a hint, not a side-effect.
 */
export interface ContextSuggestion {
  numCtx: number;
  compactLimit: number;
  rationale: string;
}

export interface RecommendedModel {
  key: string;
  label: string;
  vram: string;
  description: string;
  bestFor: string;
  contextSuggestion?: ContextSuggestion;
}

export const RECOMMENDED_MODELS: RecommendedModel[] = [
  {
    key: "qwen3.5:9b",
    label: "Qwen 3.5 · 9B",
    vram: "~7 GB",
    description:
      "The recommended default. Strong procedure-following, reliable tool calling, and correct grammar-pattern application on BM25-retrieved passages.",
    bestFor: "Most users. Daily Claude Code work on a 8–12 GB GPU.",
  },
  {
    key: "gemma4:e4b",
    label: "Gemma 4 · e4b",
    vram: "~10 GB",
    description:
      "Google Gemma 4 effective-4B variant. Cleaner structured output and more precise citations than the 9B qwen; larger VRAM footprint.",
    bestFor: "12+ GB GPUs where citation precision and output structure matter more than footprint.",
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

export interface ModelTuningParams {
  temperature?: number;
  top_p?: number;
  top_k?: number;
  presence_penalty?: number;
}

/**
 * Re-tune an already-local model in place by re-running ollama create
 * against itself with new sampling parameters baked into the manifest.
 * Pure local operation — does NOT touch the registry, so it works
 * fully offline. Typically completes in well under a second per model.
 */
export async function retuneModel(
  modelKey: string,
  params: ModelTuningParams,
): Promise<void> {
  const body = {
    model: modelKey,
    from: modelKey,
    parameters: params,
  };
  const ctrl = new AbortController();
  const timeout = setTimeout(() => ctrl.abort(), 30000);
  const res = await fetch(`${OLLAMA_URL}/api/create`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
    signal: ctrl.signal,
  });
  clearTimeout(timeout);
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`ollama create failed (${res.status}): ${text.slice(-200)}`);
  }
  // The endpoint streams NDJSON status events. Drain it so we don't
  // leave the socket half-open.
  if (res.body) {
    const reader = res.body.getReader();
    while (true) {
      const { done } = await reader.read();
      if (done) break;
    }
  }
}

/**
 * Delete a locally-pulled Ollama model. Calls Ollama's HTTP delete endpoint
 * directly — same channel as listInstalledModels and retuneModel — so it
 * works without shelling out to docker exec.
 *
 * Throws if the model doesn't exist or the daemon refuses the request.
 */
export async function deleteInstalledModel(modelKey: string): Promise<void> {
  const ctrl = new AbortController();
  const timeout = setTimeout(() => ctrl.abort(), 15000);
  const res = await fetch(`${OLLAMA_URL}/api/delete`, {
    method: "DELETE",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ model: modelKey, name: modelKey }),
    signal: ctrl.signal,
  });
  clearTimeout(timeout);
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`ollama delete failed (${res.status}): ${text.slice(-200)}`);
  }
}
