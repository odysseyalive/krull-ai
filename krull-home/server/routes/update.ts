/**
 * Repository update endpoints.
 *
 * Flow:
 *   1. Frontend POSTs /api/update
 *   2. Backend writes data/.update-requested sentinel and returns immediately
 *   3. krull-updater (sibling container) sees the sentinel, runs:
 *        git fetch + merge --ff-only + docker compose up -d --build + setup.sh
 *   4. Backend exposes /api/version (current commit) and /api/update/status
 *      (parsed from data/.update-status) for the frontend to poll
 *   5. When the rebuild touches krull-home, the API drops out from under
 *      the frontend; the frontend just keeps polling /api/version until
 *      it returns a (different) commit hash, then reloads.
 */
import { Router } from "express";
import path from "node:path";
import fs from "node:fs/promises";
import { readGitInfo } from "../lib/git.js";

const router = Router();
const REPO = process.env.KRULL_REPO ?? "/workspace";
const SENTINEL = path.join(REPO, "data", ".update-requested");
const STATUS = path.join(REPO, "data", ".update-status");
const LOG = path.join(REPO, "data", ".update-log");

router.get("/version", async (_req, res) => {
  const info = await readGitInfo(REPO);
  if (!info) {
    res.status(500).json({ error: "could not read git info" });
    return;
  }
  res.json(info);
});

router.post("/update", async (_req, res) => {
  // Refuse if an update is already in progress.
  try {
    const existing = await fs.readFile(STATUS, "utf8");
    const parsed = JSON.parse(existing) as { phase?: string };
    if (parsed.phase === "running") {
      res.status(409).json({ error: "an update is already in progress" });
      return;
    }
  } catch {
    /* no status file yet — fine */
  }

  await fs.mkdir(path.dirname(SENTINEL), { recursive: true });
  await fs.writeFile(SENTINEL, new Date().toISOString(), "utf8");
  res.json({ ok: true, requestedAt: new Date().toISOString() });
});

router.get("/update/status", async (_req, res) => {
  try {
    const text = await fs.readFile(STATUS, "utf8");
    res.json(JSON.parse(text));
  } catch {
    res.json({ phase: "idle" });
  }
});

router.get("/update/log", async (_req, res) => {
  try {
    const text = await fs.readFile(LOG, "utf8");
    res.type("text/plain").send(text);
  } catch {
    res.type("text/plain").send("(no log yet)");
  }
});

export default router;
