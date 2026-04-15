import { Router } from "express";
import path from "node:path";
import {
  RECOMMENDED_MODELS,
  listInstalledModels,
  deleteInstalledModel,
  computeContextSuggestion,
} from "../lib/models.js";
import { createJob } from "../lib/jobs.js";
import { selectActiveModel, startModelPull } from "../lib/modelInstaller.js";
import { readEnvFile, getValue } from "../lib/envFile.js";
import { detectHardware } from "../lib/hardware.js";

const router = Router();
const REPO = process.env.KRULL_REPO ?? "/workspace";

router.get("/models", async (_req, res) => {
  const [installed, env, hardware] = await Promise.all([
    listInstalledModels(),
    readEnvFile(path.join(REPO, ".env")),
    detectHardware(),
  ]);
  const installedSet = new Set(installed);
  const active = getValue(env, "OLLAMA_MODEL") ?? "";
  res.json({
    active,
    recommended: RECOMMENDED_MODELS.map((m) => ({
      ...m,
      installed: installedSet.has(m.key),
      active: m.key === active,
      contextSuggestion: computeContextSuggestion(m, hardware.gpu),
    })),
    other: installed.filter(
      (name) => !RECOMMENDED_MODELS.some((m) => m.key === name),
    ),
  });
});

router.post("/models/install", async (req, res) => {
  const body = req.body as { key?: string } | undefined;
  if (!body || typeof body.key !== "string") {
    res.status(400).json({ error: "expected { key }" });
    return;
  }
  const recommended = RECOMMENDED_MODELS.find((m) => m.key === body.key);
  if (!recommended) {
    res.status(404).json({ error: `unknown recommended model: ${body.key}` });
    return;
  }
  const job = createJob("model", recommended.key);
  startModelPull(job, recommended.key);
  res.json({ jobId: job.id, key: recommended.key });
});

/**
 * Remove a locally-pulled model. Refuses to delete the currently active
 * model — the user must switch to a different brain first. Mirrors the
 * library /api/library/delete pattern but goes through Ollama's HTTP
 * delete endpoint rather than the library installer.
 */
router.delete("/models/:key", async (req, res) => {
  const key = req.params.key;
  // Same regex selectActiveModel uses to gate yaml substitution. Keeps
  // the model name from doing anything weird if it ever flows into a
  // shell or path context.
  if (!/^[\w./:-]+$/.test(key)) {
    res.status(400).json({ error: `unsafe model key: ${key}` });
    return;
  }
  const [installed, env] = await Promise.all([
    listInstalledModels(),
    readEnvFile(path.join(REPO, ".env")),
  ]);
  if (!installed.includes(key)) {
    res.status(404).json({ error: `model is not installed: ${key}` });
    return;
  }
  const active = getValue(env, "OLLAMA_MODEL") ?? "";
  if (active === key) {
    res.status(409).json({
      error: `refusing to delete the active model. Switch to a different brain first, then delete this one.`,
    });
    return;
  }
  try {
    await deleteInstalledModel(key);
  } catch (err) {
    res.status(500).json({ error: (err as Error).message });
    return;
  }
  res.json({ ok: true, deleted: key });
});

router.post("/models/select", async (req, res) => {
  const body = req.body as { key?: string } | undefined;
  if (!body || typeof body.key !== "string") {
    res.status(400).json({ error: "expected { key }" });
    return;
  }
  // Allow both recommended and previously-pulled custom models.
  const installed = await listInstalledModels();
  if (!installed.includes(body.key)) {
    res.status(400).json({
      error: `model is not installed: ${body.key}. Pull it first.`,
    });
    return;
  }
  try {
    await selectActiveModel(body.key);
  } catch (err) {
    res.status(500).json({ error: (err as Error).message });
    return;
  }
  res.json({ ok: true, active: body.key });
});

export default router;
