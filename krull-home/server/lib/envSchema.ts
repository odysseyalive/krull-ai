/**
 * Schema describing well-known .env keys: their group, type, default, and
 * the help text shown next to the input. Pulled from .env.sample. Unknown
 * keys still appear in the editor as a "Custom" group with raw text inputs.
 *
 * The `affects` array on each field is load-bearing: on save, the env route
 * recreates exactly the union of affected containers so the running config
 * matches what the page shows. Compose passes most of these as `${VAR}`
 * interpolations baked at create-time, so a *recreate* (not a plain restart)
 * is what actually applies a changed value — see routes/env.ts.
 */
export type EnvFieldKind = "text" | "number" | "secret";

export interface EnvField {
  key: string;
  label: string;
  description: string;
  kind: EnvFieldKind;
  default?: string;
  group: string;
  /** Container names that should be recreated when this key changes. */
  affects: string[];
  /**
   * Per-container override of the environment variable NAME this key maps to
   * inside that container, when compose renames it. Defaults to the key
   * itself. Example: OLLAMA_NUM_CTX is passed to krull-ollama as
   * OLLAMA_CONTEXT_LENGTH, so it declares { "krull-ollama": "OLLAMA_CONTEXT_LENGTH" }.
   */
  containerEnvNames?: Record<string, string>;
}

export const ENV_SCHEMA: EnvField[] = [
  {
    key: "OLLAMA_MODEL",
    label: "Default model",
    description:
      "Default model used by ./krull pull-model when no argument is given.",
    kind: "text",
    default: "gemma4:e2b",
    group: "Ollama",
    affects: ["krull-ollama"],
  },
  {
    key: "OLLAMA_NUM_CTX",
    label: "Context window",
    description:
      "Token context window. Size it to your VRAM — the page suggests a value from detected GPU memory. Too large a window on limited VRAM can make requests die silently mid-generation.",
    kind: "number",
    default: "65536",
    group: "Ollama",
    affects: ["krull-ollama", "krull-sse-proxy"],
    containerEnvNames: { "krull-ollama": "OLLAMA_CONTEXT_LENGTH" },
  },
  {
    key: "OLLAMA_PREFERRED_QUANT",
    label: "Preferred quantization",
    description:
      "Preferred quantized variant when pulling models (e.g. q4_0). Applied on the next ./krull pull-model; does not retroactively re-quantize installed models.",
    kind: "text",
    default: "q4_0",
    group: "Ollama",
    affects: [],
  },
  {
    key: "OLLAMA_FLASH_ATTENTION",
    label: "Flash attention",
    description:
      "Enable Ollama flash attention (1 = on). Reduces VRAM use and is required for quantized KV cache. Recommended on for tight-VRAM setups.",
    kind: "text",
    default: "1",
    group: "Ollama",
    affects: ["krull-ollama"],
  },
  {
    key: "OLLAMA_KV_CACHE_TYPE",
    label: "KV cache quantization",
    description:
      "Quantization for the KV cache (e.g. q8_0, q4_0, f16). q8_0 roughly halves KV-cache VRAM vs f16, letting a larger context window fit. Requires flash attention.",
    kind: "text",
    default: "q8_0",
    group: "Ollama",
    affects: ["krull-ollama"],
  },
  {
    key: "OLLAMA_NUM_PARALLEL",
    label: "Parallel request slots",
    description:
      "How many requests Ollama serves concurrently for the model — the REAL lever for running agents in parallel. Each slot needs its own KV cache, so VRAM scales as slots × context window (Ollama docs). 1 = serialize (safe, allows the biggest context); higher = agents run concurrently but each context must be smaller. The context-window suggestion below divides by this value.",
    kind: "number",
    default: "1",
    group: "Ollama",
    affects: ["krull-ollama"],
  },
  {
    key: "OLLAMA_TEMPERATURE",
    label: "Temperature",
    description:
      "0.6–0.7 for code/agent reliability, 0.8 balanced, 0.9 for more varied prose. Baked into the model on save (triggers a re-tune).",
    kind: "number",
    default: "0.6",
    group: "Model parameters",
    affects: [],
  },
  {
    key: "OLLAMA_TOP_P",
    label: "Top P",
    description: "Nucleus sampling threshold. Baked into the model on save.",
    kind: "number",
    default: "0.8",
    group: "Model parameters",
    affects: [],
  },
  {
    key: "OLLAMA_TOP_K",
    label: "Top K",
    description: "Limits sampling to the K most likely tokens. Baked into the model on save.",
    kind: "number",
    default: "20",
    group: "Model parameters",
    affects: [],
  },
  {
    key: "OLLAMA_PRESENCE_PENALTY",
    label: "Presence penalty",
    description: "Discourages topic repetition. Baked into the model on save.",
    kind: "number",
    default: "1.5",
    group: "Model parameters",
    affects: [],
  },
  {
    key: "OLLAMA_NUM_PREDICT",
    label: "Max response tokens",
    description:
      "Caps tokens generated in a single response, preventing runaway generations. Baked into the model on save (triggers a re-tune).",
    kind: "number",
    default: "4096",
    group: "Model parameters",
    affects: [],
  },
  {
    key: "CONTEXT_COMPACT_LIMIT",
    label: "Auto-compact threshold",
    description:
      "Token count at which conversation history is auto-compacted. Should be roughly 75% of the context window.",
    kind: "number",
    default: "49152",
    group: "Context management",
    affects: ["krull-sse-proxy"],
  },
  {
    key: "CONTEXT_COMPACT_ENABLED",
    label: "Enable async compaction",
    description:
      "Enable async context compaction (job queue + background summarizer). Read by the krull-claude wrapper at launch — applies on the next krull-claude session, not to a running one. Set to true or false.",
    kind: "text",
    default: "true",
    group: "Context management",
    affects: [],
  },
  {
    key: "CONTEXT_SUMMARY_MODEL",
    label: "Summary model",
    description:
      "Model used for abstractive summarization of old history. Leave empty to reuse the same model mapping the chat uses.",
    kind: "text",
    default: "",
    group: "Context management",
    affects: ["krull-sse-proxy"],
  },
  {
    key: "KRULL_TOOL_CALL_HARD_CAP",
    label: "Tool-call hard cap",
    description:
      "Maximum tool-calls allowed per conversation turn before the proxy forces a final answer. Guards against tool-call loops.",
    kind: "number",
    default: "8",
    group: "Agents",
    affects: ["krull-sse-proxy"],
  },
  // NOTE: AGENT_MAX_PARALLEL, KRULL_AGENT_TOKEN_BUDGET and KRULL_AGENT_TIMEOUT_SECONDS
  // were removed — they were written to the per-session meta JSON but no consumer
  // (proxy, krull-claude launcher, or Claude Code) ever read them. Real per-model
  // request concurrency is OLLAMA_NUM_PARALLEL (see the Ollama group above).
  {
    key: "WEBUI_SECRET_KEY",
    label: "Open WebUI secret key",
    description:
      "Used for session tokens. Generate a real value for any non-local deployment.",
    kind: "secret",
    default: "changeme-generate-a-real-key",
    group: "Security",
    affects: ["krull-webui"],
  },
  {
    key: "LITELLM_MASTER_KEY",
    label: "LiteLLM master key",
    description: "API key Claude Code uses to authenticate with the gateway.",
    kind: "secret",
    default: "sk-local-dev-key",
    group: "Security",
    affects: ["krull-litellm"],
  },
  {
    key: "PHOTON_COUNTRY_CODE",
    label: "Geocoding country",
    description:
      "Restrict Photon geocoding to one country (ISO 3166-1 alpha-2). Leave empty for worldwide.",
    kind: "text",
    default: "",
    group: "Geocoding",
    affects: ["krull-photon"],
  },
  {
    key: "FAA_EDITION",
    label: "FAA chart edition",
    description:
      "FAA VFR Sectional Chart edition date. Updates every 56 days. Check aeronav.faa.gov/visual.",
    kind: "text",
    default: "03-19-2026",
    group: "FAA charts",
    affects: ["krull-tileserver"],
  },
];

export function affectedContainersFor(changedKeys: string[]): string[] {
  const out = new Set<string>();
  for (const k of changedKeys) {
    const field = ENV_SCHEMA.find((f) => f.key === k);
    if (field) for (const c of field.affects) out.add(c);
  }
  return [...out];
}

/**
 * Sampling / generation parameters that ollama bakes into a model at
 * create-time via the Modelfile. Changing any of these in .env requires
 * re-tuning every installed model — they aren't read per-request, so just
 * writing the new value to .env has no effect on what ollama serves.
 */
export const MODEL_TUNING_KEYS = [
  "OLLAMA_TEMPERATURE",
  "OLLAMA_TOP_P",
  "OLLAMA_TOP_K",
  "OLLAMA_PRESENCE_PENALTY",
  "OLLAMA_NUM_PREDICT",
] as const;

export function changedKeysRequireRetune(changedKeys: string[]): boolean {
  return changedKeys.some((k) => (MODEL_TUNING_KEYS as readonly string[]).includes(k));
}
