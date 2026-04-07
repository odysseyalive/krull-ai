/**
 * Schema describing well-known .env keys: their group, type, default, and
 * the help text shown next to the input. Pulled from .env.sample. Unknown
 * keys still appear in the editor as a "Custom" group with raw text inputs.
 */
export type EnvFieldKind = "text" | "number" | "secret";

export interface EnvField {
  key: string;
  label: string;
  description: string;
  kind: EnvFieldKind;
  default?: string;
  group: string;
  /** Container names that should be restarted when this key changes. */
  affects: string[];
}

export const ENV_SCHEMA: EnvField[] = [
  {
    key: "OLLAMA_MODEL",
    label: "Default model",
    description:
      "Default model used by ./krull pull-model when no argument is given.",
    kind: "text",
    default: "frob/qwen3.5-instruct:9b",
    group: "Ollama",
    affects: ["krull-ollama"],
  },
  {
    key: "OLLAMA_NUM_CTX",
    label: "Context window",
    description: "Token context window. Match your model's max context.",
    kind: "number",
    default: "131072",
    group: "Ollama",
    affects: ["krull-ollama", "krull-sse-proxy"],
  },
  {
    key: "OLLAMA_TEMPERATURE",
    label: "Temperature",
    description:
      "0.6–0.7 for code, 0.8 balanced, 0.9 for more varied prose.",
    kind: "number",
    default: "0.8",
    group: "Model parameters",
    affects: [],
  },
  {
    key: "OLLAMA_TOP_P",
    label: "Top P",
    description: "Nucleus sampling threshold.",
    kind: "number",
    default: "0.8",
    group: "Model parameters",
    affects: [],
  },
  {
    key: "OLLAMA_TOP_K",
    label: "Top K",
    description: "Limits sampling to the K most likely tokens.",
    kind: "number",
    default: "20",
    group: "Model parameters",
    affects: [],
  },
  {
    key: "OLLAMA_PRESENCE_PENALTY",
    label: "Presence penalty",
    description: "Discourages topic repetition.",
    kind: "number",
    default: "1.5",
    group: "Model parameters",
    affects: [],
  },
  {
    key: "CONTEXT_COMPACT_LIMIT",
    label: "Auto-compact threshold",
    description:
      "Token count at which conversation history is auto-compacted. Should be roughly 75% of OLLAMA_NUM_CTX.",
    kind: "number",
    default: "98304",
    group: "Context management",
    affects: ["krull-sse-proxy"],
  },
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
