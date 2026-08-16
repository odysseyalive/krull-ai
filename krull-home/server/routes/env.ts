import { Router } from "express";
import path from "node:path";
import { listEntries, readEnvFile, setValue, writeEnvFile, getValue } from "../lib/envFile.js";
import {
  ENV_SCHEMA,
  affectedContainersFor,
  changedKeysRequireRetune,
} from "../lib/envSchema.js";
import { createJob } from "../lib/jobs.js";
import { startModelRetune } from "../lib/modelInstaller.js";
import { isRecreatable, recreateContainerWithEnv } from "../lib/docker.js";

const router = Router();

const REPO = process.env.KRULL_REPO ?? "/workspace";
const ENV_PATH = path.join(REPO, ".env");

router.get("/env", async (_req, res) => {
  const parsed = await readEnvFile(ENV_PATH);
  const entries = listEntries(parsed);
  // Return both schema (so the UI knows what to render) and current values.
  res.json({
    path: ENV_PATH,
    schema: ENV_SCHEMA,
    values: Object.fromEntries(entries.map((e) => [e.key, e.value])),
    extras: entries
      .filter((e) => !ENV_SCHEMA.some((f) => f.key === e.key))
      .map((e) => e.key),
  });
});

router.put("/env", async (req, res) => {
  const body = req.body as { values?: Record<string, string> } | undefined;
  if (!body || typeof body.values !== "object") {
    res.status(400).json({ error: "expected { values: { KEY: 'value' } }" });
    return;
  }
  const parsed = await readEnvFile(ENV_PATH);
  const before = Object.fromEntries(listEntries(parsed).map((e) => [e.key, e.value]));
  const changed: string[] = [];
  for (const [key, val] of Object.entries(body.values)) {
    if (typeof val !== "string") continue;
    if (!/^[A-Z_][A-Z0-9_]*$/i.test(key)) continue; // safety: only valid env keys
    const existedBefore = Object.prototype.hasOwnProperty.call(before, key);
    // Skip writing an empty value for a key that wasn't in the file —
    // the form renders schema fields blank when they're absent, and submitting
    // them as "" would pollute .env with KEY="" lines.
    if (!existedBefore && val === "") continue;
    if (before[key] !== val) {
      setValue(parsed, key, val);
      changed.push(key);
    }
  }
  await writeEnvFile(ENV_PATH, parsed);

  const fresh = await readEnvFile(ENV_PATH);

  // Apply container-affecting changes by recreating each affected container
  // with the new value(s) injected. Compose bakes ${VAR} values at
  // create-time, so a recreate — not a restart — is what makes the running
  // config match what the page just saved. Done before the model re-tune so
  // a freshly-recreated ollama is up before /api/create runs against it.
  const affected = affectedContainersFor(changed);
  const recreated: string[] = [];
  const recreateErrors: Record<string, string> = {};
  for (const container of affected) {
    if (!isRecreatable(container)) continue;
    const overrides: Record<string, string> = {};
    for (const key of changed) {
      const field = ENV_SCHEMA.find((f) => f.key === key);
      if (!field || !field.affects.includes(container)) continue;
      const envName = field.containerEnvNames?.[container] ?? key;
      overrides[envName] = getValue(fresh, key) ?? "";
    }
    if (Object.keys(overrides).length === 0) continue;
    try {
      await recreateContainerWithEnv(container, overrides);
      recreated.push(container);
    } catch (err) {
      recreateErrors[container] = (err as Error).message;
    }
  }

  // Sampling parameters (temperature, top_p, top_k, presence_penalty)
  // are baked into each Ollama model at create-time, NOT read at
  // request-time, so just writing them to .env has no runtime effect.
  // When any of those keys changed, kick off a job that calls
  // ollama's local /api/create endpoint for every installed model
  // with the new parameters. The frontend can stream this job to
  // show progress.
  let retuneJobId: string | undefined;
  if (changedKeysRequireRetune(changed)) {
    const params = {
      temperature: numberOrUndefined(getValue(fresh, "OLLAMA_TEMPERATURE")),
      top_p: numberOrUndefined(getValue(fresh, "OLLAMA_TOP_P")),
      top_k: numberOrUndefined(getValue(fresh, "OLLAMA_TOP_K")),
      presence_penalty: numberOrUndefined(getValue(fresh, "OLLAMA_PRESENCE_PENALTY")),
      num_predict: numberOrUndefined(getValue(fresh, "OLLAMA_NUM_PREDICT")),
    };
    const job = createJob("model-retune", "all-installed");
    retuneJobId = job.id;
    // Fire-and-forget: the job streams its own progress through the
    // events emitter; the frontend subscribes via /api/jobs/:id/stream.
    void startModelRetune(job, params);
  }

  res.json({
    ok: true,
    changed,
    affects: affected,
    recreated,
    recreateErrors,
    retuneJobId,
  });
});

function numberOrUndefined(s: string | undefined): number | undefined {
  if (s === undefined || s === "") return undefined;
  const n = Number(s);
  return Number.isFinite(n) ? n : undefined;
}

export default router;
