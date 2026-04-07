import { Router } from "express";
import path from "node:path";
import { RECOMMENDED_MODELS, listInstalledModels } from "../lib/models.js";
import { createJob } from "../lib/jobs.js";
import { selectActiveModel, startModelPull } from "../lib/modelInstaller.js";
import { readEnvFile, getValue } from "../lib/envFile.js";

const router = Router();
const REPO = process.env.KRULL_REPO ?? "/workspace";

router.get("/models", async (_req, res) => {
  const [installed, env] = await Promise.all([
    listInstalledModels(),
    readEnvFile(path.join(REPO, ".env")),
  ]);
  const installedSet = new Set(installed);
  const active = getValue(env, "OLLAMA_MODEL") ?? "";
  res.json({
    active,
    recommended: RECOMMENDED_MODELS.map((m) => ({
      ...m,
      installed: installedSet.has(m.key),
      active: m.key === active,
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
