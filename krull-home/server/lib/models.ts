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
  /** Approximate VRAM occupied by the weights alone (bytes). Used to
   * compute how much VRAM is left for the KV cache. */
  weightBytes: number;
  /** Approximate KV cache footprint per context token at Ollama's
   * default KV quantization (fp16). Rough estimate — drives the
   * dynamic context suggestion. */
  kvBytesPerToken: number;
  /** Model's native context window — the suggestion never exceeds this
   * even when VRAM would technically allow more. */
  nativeMaxCtx: number;
  contextSuggestion?: ContextSuggestion;
}

export const RECOMMENDED_MODELS: RecommendedModel[] = [
  {
    key: "gemma4:e2b",
    label: "Gemma 4 · e2b",
    vram: "~4 GB",
    description:
      "Google Gemma 4 effective-2B variant. Light footprint, fast first-token latency, clean structured output. The small-VRAM default.",
    bestFor: "4–8 GB GPUs, laptops, or anyone who wants snappy responses over maximum depth.",
    weightBytes: 4 * 1024 * 1024 * 1024,
    kvBytesPerToken: 70 * 1024,
    nativeMaxCtx: 131072,
  },
  {
    key: "gemma4:e4b",
    label: "Gemma 4 · e4b",
    vram: "~10 GB",
    description:
      "Google Gemma 4 effective-4B variant. Sharper reasoning and more precise citations than e2b; larger VRAM footprint.",
    bestFor: "12+ GB GPUs where citation precision and output structure matter more than footprint.",
    weightBytes: 10 * 1024 * 1024 * 1024,
    kvBytesPerToken: 120 * 1024,
    nativeMaxCtx: 131072,
  },
];

/**
 * Context window + auto-compact threshold suggestion, computed from the
 * host's free VRAM after subtracting the model weights and a runtime
 * overhead. Falls back to a conservative 8k window when GPU VRAM is not
 * detectable (Apple Silicon, CPU-only hosts).
 *
 * The math is deliberately rough: Ollama's actual KV cache footprint
 * shifts with KV quantization, flash attention, and how Ollama batches
 * prefill. We apply an 85% safety margin on the computed token count
 * so minor estimation error doesn't push the user over the edge into
 * an OOM during a long conversation.
 */
export function computeContextSuggestion(
  model: RecommendedModel,
  gpu: { vendor: "nvidia" | "none"; totalBytes?: number; name?: string },
): ContextSuggestion {
  const gbFmt = (bytes: number): string =>
    `${(bytes / (1024 * 1024 * 1024)).toFixed(1)} GB`;
  const tokenFmt = (n: number): string => `${Math.round(n / 1024)}k tokens`;
  const roundToTier = (n: number): number => {
    const tiers = [131072, 65536, 32768, 16384, 8192, 4096, 2048];
    for (const t of tiers) if (n >= t) return t;
    return 2048;
  };

  // Fallback: no detected GPU — suggest a conservative 8k window so
  // the user at least gets a sensible starting point.
  if (gpu.vendor === "none" || !gpu.totalBytes) {
    const numCtx = 8192;
    return {
      numCtx,
      compactLimit: Math.round(numCtx * 0.75),
      rationale:
        `No GPU detected, so this is a conservative CPU-friendly default. ` +
        `If you're on Apple Silicon, set OLLAMA_NUM_CTX manually — unified ` +
        `memory can usually go much higher than 8k, but krull-home can't ` +
        `introspect Metal from inside the container. Auto-compact at 75%.`,
    };
  }

  // Budget: total VRAM minus weights minus ~1 GB runtime overhead
  // (CUDA kernels, Ollama allocator slack, fragmentation).
  const overheadBytes = 1 * 1024 * 1024 * 1024;
  const kvBudget = Math.max(
    0,
    gpu.totalBytes - model.weightBytes - overheadBytes,
  );
  const rawMaxTokens = kvBudget / model.kvBytesPerToken;
  // 85% safety margin on the computed KV token count.
  const safeMaxTokens = Math.floor(rawMaxTokens * 0.85);
  const numCtx = Math.min(model.nativeMaxCtx, roundToTier(safeMaxTokens));
  const compactLimit = Math.round(numCtx * 0.75);

  const rationale =
    `Your ${gpu.name ?? "GPU"} reports ${gbFmt(gpu.totalBytes)} total VRAM. ` +
    `After the ${model.label} weights (~${gbFmt(model.weightBytes)}) and ` +
    `~1 GB runtime overhead, ${gbFmt(kvBudget)} is left for the KV cache. ` +
    `At ~${Math.round(model.kvBytesPerToken / 1024)} KB per token that fits ` +
    `roughly ${tokenFmt(rawMaxTokens)}; rounded down to a friendly size with ` +
    `an 85% safety margin gives ${tokenFmt(numCtx)}` +
    (numCtx >= model.nativeMaxCtx
      ? ` — capped at Gemma 4's native 128k window.`
      : `.`) +
    ` Auto-compact fires at 75% (${tokenFmt(compactLimit)}) so the model ` +
    `never bumps the hard wall mid-turn.`;

  return { numCtx, compactLimit, rationale };
}

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
