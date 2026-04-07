import { Router } from "express";
import { loadCatalog, type CatalogPackage } from "../lib/catalog.js";
import { createJob, getJob } from "../lib/jobs.js";
import { deletePackage, startInstall, affectedContainer } from "../lib/installer.js";
import { restartContainer, isRestartable } from "../lib/docker.js";

const router = Router();
const REPO = process.env.KRULL_REPO ?? "/workspace";

router.get("/library", async (_req, res) => {
  try {
    const catalog = await loadCatalog(REPO);
    res.json(catalog);
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    res.status(500).json({ error: message });
  }
});

async function findPackage(
  kind: string,
  key: string,
): Promise<CatalogPackage | undefined> {
  const catalog = await loadCatalog(REPO);
  return catalog.packages.find((p) => p.kind === kind && p.key === key);
}

router.post("/library/install", async (req, res) => {
  const body = req.body as { kind?: string; key?: string } | undefined;
  if (!body || typeof body.kind !== "string" || typeof body.key !== "string") {
    res.status(400).json({ error: "expected { kind, key }" });
    return;
  }
  const pkg = await findPackage(body.kind, body.key);
  if (!pkg) {
    res.status(404).json({ error: `unknown package: ${body.kind}/${body.key}` });
    return;
  }
  const job = createJob(pkg.kind, pkg.key);
  startInstall(job, pkg);
  res.json({ jobId: job.id, kind: pkg.kind, key: pkg.key });
});

router.post("/library/delete", async (req, res) => {
  const body = req.body as { kind?: string; key?: string } | undefined;
  if (!body || typeof body.kind !== "string" || typeof body.key !== "string") {
    res.status(400).json({ error: "expected { kind, key }" });
    return;
  }
  const pkg = await findPackage(body.kind, body.key);
  if (!pkg) {
    res.status(404).json({ error: `unknown package: ${body.kind}/${body.key}` });
    return;
  }
  if (!pkg.installed) {
    res.status(400).json({ error: "package is not installed" });
    return;
  }
  try {
    await deletePackage(pkg);
  } catch (err) {
    res.status(500).json({ error: (err as Error).message });
    return;
  }
  // Auto-restart the affected container so the service notices the file is gone.
  const container = affectedContainer(pkg.kind);
  let restarted = false;
  if (container && isRestartable(container)) {
    try {
      await restartContainer(container);
      restarted = true;
    } catch (err) {
      res.status(500).json({
        error: `deleted ${pkg.file} but failed to restart ${container}: ${(err as Error).message}`,
      });
      return;
    }
  }
  res.json({ ok: true, container, restarted });
});

router.get("/jobs/:id/stream", (req, res) => {
  const job = getJob(req.params.id);
  if (!job) {
    res.status(404).json({ error: "no such job" });
    return;
  }
  res.set({
    "Content-Type": "text/event-stream",
    "Cache-Control": "no-cache, no-transform",
    Connection: "keep-alive",
    "X-Accel-Buffering": "no",
  });
  res.flushHeaders();

  const send = (event: unknown) => {
    res.write(`data: ${JSON.stringify(event)}\n\n`);
  };

  // Replay history so a late subscriber catches up
  for (const ev of job.events) send(ev);

  // Live updates
  const onEvent = (ev: unknown) => {
    send(ev);
    if (
      typeof ev === "object" &&
      ev !== null &&
      "phase" in ev &&
      (ev.phase === "done" || ev.phase === "failed")
    ) {
      // Close the stream once the job is terminal.
      res.end();
    }
  };
  job.emitter.on("event", onEvent);

  // If the job already finished before subscription, end immediately
  // after replaying history.
  if (job.phase === "done" || job.phase === "failed") {
    res.end();
    return;
  }

  req.on("close", () => {
    job.emitter.off("event", onEvent);
  });
});

export default router;
