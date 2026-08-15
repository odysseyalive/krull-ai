/**
 * Pull an Ollama model with real per-byte progress, then bake in tuned
 * sampling parameters from .env (matching scripts/pull-model.sh's defaults).
 *
 * Rewritten from a `pull-model.sh` shell-out to a direct call to Ollama's
 * HTTP /api/pull endpoint (NDJSON stream of `{status, total, completed,
 * digest}` events) so the picker UI can show real percent/byte progress
 * instead of an indeterminate stripe. The shell script is still around for
 * CLI users; this just uses the same underlying ollama HTTP API the script
 * shells out to.
 *
 * Selecting a model patches litellm/config.yaml and restarts krull-litellm.
 */
import fs from "node:fs/promises";
import path from "node:path";
import { restartContainer, isRestartable } from "./docker.js";
import { type Job, pushEvent } from "./jobs.js";
import { readEnvFile, getValue, setValue, writeEnvFile } from "./envFile.js";
import { listInstalledModels, retuneModel, type ModelTuningParams } from "./models.js";

const REPO = process.env.KRULL_REPO ?? "/workspace";
const OLLAMA_URL = process.env.OLLAMA_URL ?? "http://krull-ollama:11434";
const WEBUI_URL = process.env.WEBUI_INTERNAL_URL ?? "http://krull-webui:8080";

/**
 * Read the WebUI / SSE-proxy API key out of litellm/config.yaml. That
 * file is the canonical place the key lives in this repo (it was
 * provisioned there by ./krull setup), so reading it from there keeps
 * us from duplicating the secret in a second config. Returns null if
 * we can't find it; callers must treat the WebUI default-model update
 * as best-effort.
 */
async function readWebuiApiKey(): Promise<string | null> {
  try {
    const text = await fs.readFile(
      path.join(REPO, "litellm", "config.yaml"),
      "utf8",
    );
    const m = text.match(/api_key:\s*"([^"]+)"/);
    return m?.[1] ?? null;
  } catch {
    return null;
  }
}

/**
 * Tell Open WebUI which model to default to in its chat UI. Without
 * this, activating a brain in the picker would update LiteLLM (and
 * therefore Claude Code) but leave the WebUI dropdown pointing at
 * whatever model it last remembered — confusing for users who switch
 * between the two surfaces. Best-effort: a WebUI failure should not
 * break LiteLLM activation, since the litellm path is the more critical
 * of the two for Claude Code traffic.
 */
async function setWebuiDefaultModel(modelKey: string): Promise<void> {
  const apiKey = await readWebuiApiKey();
  if (!apiKey) {
    console.warn(
      "[selectActiveModel] could not read WebUI API key from litellm/config.yaml — skipping WebUI default update",
    );
    return;
  }
  // Read-modify-write: fetch the current config so we don't blow away
  // unrelated keys (PINNED_MODELS, MODEL_ORDER_LIST, …) that the user
  // may have set elsewhere.
  let current: Record<string, unknown> = {};
  try {
    const ctrl = new AbortController();
    const timeout = setTimeout(() => ctrl.abort(), 4000);
    const res = await fetch(`${WEBUI_URL}/api/v1/configs/models`, {
      headers: { Authorization: `Bearer ${apiKey}` },
      signal: ctrl.signal,
    });
    clearTimeout(timeout);
    if (res.ok) {
      current = (await res.json()) as Record<string, unknown>;
    }
  } catch (err) {
    console.warn(
      `[selectActiveModel] could not read WebUI default-models config: ${(err as Error).message}`,
    );
  }
  const body = { ...current, DEFAULT_MODELS: modelKey };
  try {
    const ctrl = new AbortController();
    const timeout = setTimeout(() => ctrl.abort(), 4000);
    const res = await fetch(`${WEBUI_URL}/api/v1/configs/models`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${apiKey}`,
        "content-type": "application/json",
      },
      body: JSON.stringify(body),
      signal: ctrl.signal,
    });
    clearTimeout(timeout);
    if (!res.ok) {
      const text = await res.text().catch(() => "");
      console.warn(
        `[selectActiveModel] WebUI default-models POST failed (${res.status}): ${text.slice(-200)}`,
      );
      return;
    }
    console.log(`[selectActiveModel] set WebUI default model to ${modelKey}`);
  } catch (err) {
    console.warn(
      `[selectActiveModel] WebUI default-models POST threw: ${(err as Error).message}`,
    );
  }
}

/** Defaults mirror scripts/pull-model.sh and envSchema.ts. */
const TUNING_DEFAULTS = {
  OLLAMA_TEMPERATURE: 0.8,
  OLLAMA_TOP_P: 0.8,
  OLLAMA_TOP_K: 20,
  OLLAMA_PRESENCE_PENALTY: 1.5,
};

async function tuningParamsFromEnv(): Promise<ModelTuningParams> {
  const parsed = await readEnvFile(path.join(REPO, ".env"));
  const num = (k: keyof typeof TUNING_DEFAULTS): number => {
    const raw = getValue(parsed, k);
    const n = raw !== undefined ? parseFloat(raw) : NaN;
    return Number.isFinite(n) ? n : TUNING_DEFAULTS[k];
  };
  return {
    temperature: num("OLLAMA_TEMPERATURE"),
    top_p: num("OLLAMA_TOP_P"),
    top_k: num("OLLAMA_TOP_K"),
    presence_penalty: num("OLLAMA_PRESENCE_PENALTY"),
  };
}

export function startModelPull(job: Job, modelKey: string): void {
  // Fire-and-forget — the job/SSE machinery is the source of truth for
  // anything the caller cares about, so we don't need to await this.
  void runModelPull(job, modelKey).catch((err) => {
    pushEvent(job, {
      phase: "failed",
      error: `pull crashed: ${(err as Error).message}`,
      timestamp: Date.now(),
    });
  });
}

async function runModelPull(job: Job, modelKey: string): Promise<void> {
  pushEvent(job, {
    phase: "downloading",
    percent: 0,
    bytes: 0,
    total: 0,
    message: `Starting pull of ${modelKey}…`,
    timestamp: Date.now(),
  });

  const res = await fetch(`${OLLAMA_URL}/api/pull`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ model: modelKey, name: modelKey, stream: true }),
  });
  if (!res.ok || !res.body) {
    const text = await res.text().catch(() => "");
    pushEvent(job, {
      phase: "failed",
      error: `ollama /api/pull failed (${res.status}): ${text.slice(-300)}`,
      timestamp: Date.now(),
    });
    return;
  }

  // NDJSON stream parser. ollama emits one JSON object per line; large
  // layers can produce many events per second so we throttle UI updates
  // to ~5/s by suppressing identical-percent events. The final {status:
  // "success"} (or any non-progress status) is always forwarded.
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  let lastEmittedPercent = -1;
  let lastEmitMs = 0;
  let sawSuccess = false;

  const handleLine = (line: string) => {
    if (!line.trim()) return;
    let ev: {
      status?: string;
      digest?: string;
      total?: number;
      completed?: number;
      error?: string;
    };
    try {
      ev = JSON.parse(line);
    } catch {
      // Ignore unparseable lines — usually a partial flush from the daemon.
      return;
    }
    if (ev.error) {
      pushEvent(job, {
        phase: "failed",
        error: ev.error,
        timestamp: Date.now(),
      });
      return;
    }
    if (typeof ev.total === "number" && typeof ev.completed === "number" && ev.total > 0) {
      const percent = Math.min(100, Math.floor((ev.completed / ev.total) * 100));
      const now = Date.now();
      // Throttle: only emit on percent change AND at most every 200 ms,
      // to avoid swamping the SSE stream with redundant events.
      if (percent !== lastEmittedPercent && now - lastEmitMs >= 200) {
        lastEmittedPercent = percent;
        lastEmitMs = now;
        pushEvent(job, {
          phase: "downloading",
          percent,
          bytes: ev.completed,
          total: ev.total,
          message: ev.status ?? `Pulling ${modelKey}`,
          timestamp: now,
        });
      }
    } else if (ev.status) {
      // Non-byte status updates: "pulling manifest", "verifying sha256
      // digest", "writing manifest", "success". Forward as a message-only
      // event so the label updates even when there are no bytes to count.
      pushEvent(job, {
        phase: "downloading",
        message: ev.status,
        timestamp: Date.now(),
      });
      if (ev.status === "success") sawSuccess = true;
    }
  };

  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let nl: number;
      while ((nl = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, nl);
        buf = buf.slice(nl + 1);
        handleLine(line);
      }
    }
    if (buf.length > 0) handleLine(buf);
  } catch (err) {
    pushEvent(job, {
      phase: "failed",
      error: `pull stream error: ${(err as Error).message}`,
      timestamp: Date.now(),
    });
    return;
  }

  if (!sawSuccess) {
    pushEvent(job, {
      phase: "failed",
      error: "ollama /api/pull ended without a success event",
      timestamp: Date.now(),
    });
    return;
  }

  // Pull is done — bake in the tuned sampling params, matching what the
  // pull-model.sh shell script would have done. This is local-only
  // (calls /api/create against the same daemon) and typically completes
  // in well under a second.
  pushEvent(job, {
    phase: "downloading",
    percent: 100,
    message: "Applying tuned parameters…",
    timestamp: Date.now(),
  });
  try {
    const params = await tuningParamsFromEnv();
    await retuneModel(modelKey, params);
  } catch (err) {
    pushEvent(job, {
      phase: "failed",
      error: `tuning failed: ${(err as Error).message}`,
      timestamp: Date.now(),
    });
    return;
  }

  pushEvent(job, {
    phase: "done",
    message: `Pulled ${modelKey}`,
    timestamp: Date.now(),
  });
}

/**
 * Re-tune every installed model with new sampling parameters. Runs the
 * ollama create call sequentially per model and reports per-model
 * progress through the job's event stream. Fully offline — uses
 * Ollama's local /api/create endpoint, not pull-model.sh.
 */
export async function startModelRetune(
  job: Job,
  params: ModelTuningParams,
): Promise<void> {
  const installed = await listInstalledModels();
  if (installed.length === 0) {
    pushEvent(job, {
      phase: "done",
      message: "No installed models to re-tune.",
      timestamp: Date.now(),
    });
    return;
  }

  pushEvent(job, {
    phase: "downloading",
    percent: 0,
    bytes: 0,
    total: installed.length,
    message: `Re-tuning ${installed.length} model${installed.length === 1 ? "" : "s"}…`,
    timestamp: Date.now(),
  });

  for (let i = 0; i < installed.length; i++) {
    const name = installed[i];
    pushEvent(job, {
      phase: "downloading",
      percent: Math.round((i / installed.length) * 100),
      bytes: i,
      total: installed.length,
      message: `Re-tuning ${name}…`,
      timestamp: Date.now(),
    });
    try {
      await retuneModel(name, params);
    } catch (err) {
      pushEvent(job, {
        phase: "failed",
        error: `Re-tune of ${name} failed: ${(err as Error).message}`,
        timestamp: Date.now(),
      });
      return;
    }
  }

  pushEvent(job, {
    phase: "done",
    message: `Re-tuned ${installed.length} model${installed.length === 1 ? "" : "s"}.`,
    timestamp: Date.now(),
  });
}

/**
 * Make a model the active default: write OLLAMA_MODEL into .env, patch
 * litellm/config.yaml so every claude-* mapping points at the new model,
 * restart krull-litellm so the new config takes effect, AND tell Open
 * WebUI to default new chats to the same model. Without that last step
 * the picker would update Claude Code's gateway but leave the WebUI
 * dropdown pointing at whatever it last remembered, confusing users
 * who switch between the two surfaces.
 */
export async function selectActiveModel(modelKey: string): Promise<void> {
  // Reject anything that isn't a plausible ollama model name up-front
  // so a bad key never reaches the .env write or the yaml substitution.
  if (!/^[\w./:-]+$/.test(modelKey)) {
    throw new Error(`refusing to use unsafe model key: ${modelKey}`);
  }

  // 1. Update .env
  const envPath = path.join(REPO, ".env");
  const parsed = await readEnvFile(envPath);
  setValue(parsed, "OLLAMA_MODEL", modelKey);
  await writeEnvFile(envPath, parsed);

  // 2. Patch litellm/config.yaml. Every line of the form
  //      model: openai/<something>
  //    becomes
  //      model: openai/<modelKey>
  const configPath = path.join(REPO, "litellm", "config.yaml");
  const original = await fs.readFile(configPath, "utf8");
  const patched = original.replace(
    /^(\s*model:\s*)openai\/[^\s#]+/gm,
    `$1openai/${modelKey}`,
  );
  if (patched !== original) {
    await fs.writeFile(configPath, patched, "utf8");
  }

  // 3. Restart litellm so the patched config takes effect.
  if (isRestartable("krull-litellm")) {
    await restartContainer("krull-litellm");
  }

  // 4. Tell Open WebUI to default to this brain too. Best-effort —
  //    a WebUI failure does not roll back the litellm change above,
  //    since the litellm path is the more critical of the two for
  //    Claude Code traffic and the user can correct the WebUI dropdown
  //    manually if it gets out of sync.
  await setWebuiDefaultModel(modelKey);
}
