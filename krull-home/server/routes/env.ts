import { Router } from "express";
import path from "node:path";
import { listEntries, readEnvFile, setValue, writeEnvFile } from "../lib/envFile.js";
import { ENV_SCHEMA, affectedContainersFor } from "../lib/envSchema.js";

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
  res.json({
    ok: true,
    changed,
    affects: affectedContainersFor(changed),
  });
});

export default router;
