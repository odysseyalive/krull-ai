/**
 * Install / delete operations for catalog packages. Spawns the existing
 * bash scripts (no reimplementation), polls the target file's size to
 * report download progress, and restarts the affected container on
 * completion.
 */
import { spawn } from "node:child_process";
import fs from "node:fs/promises";
import path from "node:path";
import { restartContainer, isRestartable } from "./docker.js";
import type { CatalogPackage } from "./catalog.js";
import { type Job, pushEvent } from "./jobs.js";

const REPO = process.env.KRULL_REPO ?? "/workspace";

/** Approximate sizes look like "100 MB", "~5 GB", "8.2 GB". */
export function parseSize(s: string): number | null {
  const m = /([\d.]+)\s*([KMGT]?)B/i.exec(s);
  if (!m) return null;
  const value = parseFloat(m[1]);
  if (Number.isNaN(value)) return null;
  const unit = m[2].toUpperCase();
  const mult = unit === "T" ? 1024 ** 4
    : unit === "G" ? 1024 ** 3
    : unit === "M" ? 1024 ** 2
    : unit === "K" ? 1024
    : 1;
  return Math.round(value * mult);
}

/** The container that needs restarting after a given package kind. */
export function affectedContainer(kind: CatalogPackage["kind"]): string | null {
  if (kind === "knowledge" || kind === "wikipedia") return "krull-kiwix";
  if (kind === "maps") return "krull-tileserver";
  return null;
}

function scriptFor(kind: CatalogPackage["kind"]): string {
  switch (kind) {
    case "knowledge":
      return "scripts/download-knowledge.sh";
    case "wikipedia":
      return "scripts/download-wikipedia.sh";
    case "maps":
      return "scripts/download-maps.sh";
  }
}

export function startInstall(job: Job, pkg: CatalogPackage): void {
  const script = scriptFor(pkg.kind);
  const target = path.join(REPO, pkg.targetDir, pkg.file);
  const expectedBytes = parseSize(pkg.size) ?? 0;

  pushEvent(job, {
    phase: "downloading",
    percent: 0,
    bytes: 0,
    total: expectedBytes,
    timestamp: Date.now(),
  });

  const child = spawn("bash", [script, pkg.key], {
    cwd: REPO,
    env: process.env,
    stdio: ["ignore", "pipe", "pipe"],
  });

  let stderrTail = "";
  child.stderr.on("data", (chunk: Buffer) => {
    stderrTail = (stderrTail + chunk.toString()).slice(-2000);
  });
  child.stdout.on("data", () => {
    /* swallow — progress is reported via file-size polling */
  });

  const poll = setInterval(async () => {
    try {
      const st = await fs.stat(target);
      const bytes = st.size;
      const percent = expectedBytes
        ? Math.min(99, Math.round((bytes / expectedBytes) * 100))
        : undefined;
      pushEvent(job, {
        phase: "downloading",
        percent,
        bytes,
        total: expectedBytes,
        timestamp: Date.now(),
      });
    } catch {
      // file not yet present
    }
  }, 600);

  child.on("close", async (code) => {
    clearInterval(poll);
    if (code !== 0) {
      pushEvent(job, {
        phase: "failed",
        error: `script exited ${code}: ${stderrTail.trim().slice(-400)}`,
        timestamp: Date.now(),
      });
      return;
    }

    try {
      const st = await fs.stat(target);
      pushEvent(job, {
        phase: "downloading",
        percent: 100,
        bytes: st.size,
        total: st.size,
        timestamp: Date.now(),
      });
    } catch {
      /* ignore */
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
          error: `restart failed: ${(err as Error).message}`,
          timestamp: Date.now(),
        });
        return;
      }
    }

    pushEvent(job, {
      phase: "done",
      message: `Installed ${pkg.name}`,
      timestamp: Date.now(),
    });
  });

  child.on("error", (err) => {
    clearInterval(poll);
    pushEvent(job, {
      phase: "failed",
      error: `spawn failed: ${err.message}`,
      timestamp: Date.now(),
    });
  });
}

export async function deletePackage(pkg: CatalogPackage): Promise<void> {
  const allowedDirs = ["zim", "data/tiles"];
  if (!allowedDirs.includes(pkg.targetDir)) {
    throw new Error(`refusing to delete from non-allowed dir: ${pkg.targetDir}`);
  }
  const fullDir = path.resolve(REPO, pkg.targetDir);
  const full = path.resolve(fullDir, pkg.file);
  if (!full.startsWith(fullDir + path.sep)) {
    throw new Error(`refusing to delete file outside ${pkg.targetDir}: ${full}`);
  }
  await fs.unlink(full);
}
