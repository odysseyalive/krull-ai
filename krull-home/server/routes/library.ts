import { Router } from "express";
import { loadCatalog, type CatalogPackage, type CatalogBundle } from "../lib/catalog.js";
import { createJob, getJob, pushEvent } from "../lib/jobs.js";
import {
  deletePackage,
  startInstall,
  startBundleInstall,
  affectedContainer,
} from "../lib/installer.js";
import { restartContainer, isRestartable } from "../lib/docker.js";
import { installQueue } from "../lib/installQueue.js";

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

async function findBundle(
  kind: string,
  key: string,
): Promise<CatalogBundle | undefined> {
  const catalog = await loadCatalog(REPO);
  return catalog.bundles.find((b) => b.kind === kind && b.key === key);
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
  const position = installQueue.enqueue(job, pkg.name, () => startInstall(job, pkg));
  res.json({ jobId: job.id, kind: pkg.kind, key: pkg.key, position });
});

router.post("/library/install-bundle", async (req, res) => {
  const body = req.body as { kind?: string; key?: string } | undefined;
  if (!body || typeof body.kind !== "string" || typeof body.key !== "string") {
    res.status(400).json({ error: "expected { kind, key }" });
    return;
  }
  const bundle = await findBundle(body.kind, body.key);
  if (!bundle) {
    res.status(404).json({ error: `unknown bundle: ${body.kind}/${body.key}` });
    return;
  }
  const job = createJob(`${bundle.kind}-bundle`, bundle.key);
  const position = installQueue.enqueue(job, `${bundle.name} (bundle)`, () =>
    startBundleInstall(job, bundle.key, bundle.members.length, bundle.members),
  );
  res.json({ jobId: job.id, kind: bundle.kind, key: bundle.key, position });
});

router.get("/library/queue", (_req, res) => {
  res.json(installQueue.snapshot());
});

router.get("/library/log", async (_req, res) => {
  // Persistent log of every install/delete result, including failures.
  // Survives container restarts and the in-memory job GC. Each line
  // looks like: 2026-04-08T22:11:33.456Z FAIL knowledge/devdocs-react — <reason>
  try {
    const fs = await import("node:fs/promises");
    const path = await import("node:path");
    const logPath = path.join(REPO, "data", ".install-log");
    const text = await fs.readFile(logPath, "utf8");
    res.type("text/plain").send(text);
  } catch (err) {
    const e = err as NodeJS.ErrnoException;
    if (e.code === "ENOENT") {
      res.type("text/plain").send("(no install log yet)");
      return;
    }
    res.status(500).type("text/plain").send(`error reading log: ${e.message}`);
  }
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

  // Run deletes through the same queue as installs so a delete that
  // races against an in-progress install doesn't restart kiwix in the
  // middle of the install.
  const job = createJob(`${pkg.kind}-delete`, pkg.key);
  const position = installQueue.enqueue(job, `Delete ${pkg.name}`, async () => {
    pushEvent(job, {
      phase: "downloading",
      message: `Deleting ${pkg.name}…`,
      timestamp: Date.now(),
    });
    try {
      await deletePackage(pkg);
    } catch (err) {
      pushEvent(job, {
        phase: "failed",
        error: (err as Error).message,
        timestamp: Date.now(),
      });
      return;
    }
    const container = affectedContainer(pkg.kind);
    if (container && isRestartable(container)) {
      pushEvent(job, {
        phase: "restarting",
        message: `Restarting ${container}`,
        timestamp: Date.now(),
      });
      try {
        await restartContainer(container);
      } catch (err) {
        pushEvent(job, {
          phase: "failed",
          error: `deleted ${pkg.file} but failed to restart ${container}: ${(err as Error).message}`,
          timestamp: Date.now(),
        });
        return;
      }
    }
    pushEvent(job, {
      phase: "done",
      message: `Deleted ${pkg.name}`,
      timestamp: Date.now(),
    });
  });
  res.json({ jobId: job.id, kind: pkg.kind, key: pkg.key, position });
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
