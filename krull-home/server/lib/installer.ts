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
import { loadCatalog as loadCatalogForCleanup, type CatalogPackage } from "./catalog.js";
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

    // Sanity-check the file actually contains what we expect. The
    // download mirror sometimes serves an HTML 404 page when a catalog
    // entry has drifted from upstream — curl saves it under the .zim
    // path and kiwix-serve refuses to start. Catch this here, delete
    // the bad file, and report a useful error instead of letting it
    // poison the zim/ directory.
    try {
      const st = await fs.stat(target);
      const validation = await validateDownload(target, pkg);
      if (!validation.ok) {
        await fs.unlink(target).catch(() => {});
        pushEvent(job, {
          phase: "failed",
          error: validation.reason,
          timestamp: Date.now(),
        });
        return;
      }
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

/**
 * Install a knowledge bundle by spawning download-knowledge.sh with the
 * bundle name. The script knows how to expand bundles into their member
 * packages and downloads them sequentially in one bash process. We
 * parse stdout for the script's per-package "Downloading: ..." log
 * lines to drive an "Installing X of N" progress display.
 */
export async function startBundleInstall(
  job: Job,
  bundleKey: string,
  totalCount: number,
  memberKeys: string[] = [],
): Promise<void> {
  pushEvent(job, {
    phase: "downloading",
    percent: 0,
    bytes: 0,
    total: totalCount,
    message: `Installing bundle ${bundleKey}…`,
    timestamp: Date.now(),
  });

  // Pre-clean: remove any corrupt files for members of this bundle
  // BEFORE running the script. The script's [ -f ] "already downloaded"
  // check would otherwise shortcircuit past corrupt files forever, and
  // we'd loop on the same broken state on every click. We do this by
  // re-reading the catalog (so we have the corrupt flag) and unlinking
  // any matching files.
  if (memberKeys.length > 0) {
    try {
      const catalog = await loadCatalogForCleanup(REPO);
      const cleaned: string[] = [];
      for (const key of memberKeys) {
        const pkg = catalog.packages.find((p) => p.key === key);
        if (pkg && pkg.corrupt) {
          const full = path.join(REPO, pkg.targetDir, pkg.file);
          await fs.unlink(full).catch(() => {});
          cleaned.push(`${key} (${pkg.corrupt})`);
        }
      }
      if (cleaned.length > 0) {
        pushEvent(job, {
          phase: "downloading",
          percent: 0,
          bytes: 0,
          total: totalCount,
          message: `Cleaned ${cleaned.length} corrupt file${cleaned.length === 1 ? "" : "s"}: ${cleaned.join(", ")}`,
          timestamp: Date.now(),
        });
      }
    } catch {
      /* If catalog scan fails we just proceed without pre-clean */
    }
  }

  const child = spawn("bash", ["scripts/download-knowledge.sh", bundleKey], {
    cwd: REPO,
    env: process.env,
    stdio: ["ignore", "pipe", "pipe"],
  });

  let stderrTail = "";
  let stdoutBuf = "";
  // Track how many distinct packages have started so we can emit
  // "Installing X of N: <desc>" updates as the bash script chews
  // through the bundle.
  let started = 0;

  child.stderr.on("data", (chunk: Buffer) => {
    stderrTail = (stderrTail + chunk.toString()).slice(-2000);
  });

  child.stdout.on("data", (chunk: Buffer) => {
    stdoutBuf += chunk.toString();
    let nl: number;
    while ((nl = stdoutBuf.indexOf("\n")) !== -1) {
      const line = stdoutBuf.slice(0, nl);
      stdoutBuf = stdoutBuf.slice(nl + 1);

      // Lines like: [*] Downloading: Python standard library docs (4 MB)
      const downloading = line.match(/^\[\*\] Downloading: (.+?) \(/);
      if (downloading) {
        started++;
        pushEvent(job, {
          phase: "downloading",
          percent: totalCount
            ? Math.round(((started - 1) / totalCount) * 100)
            : undefined,
          bytes: started - 1,
          total: totalCount,
          message: `Installing ${started} of ${totalCount}: ${downloading[1]}`,
          timestamp: Date.now(),
        });
        continue;
      }

      // Lines like: [+] Already downloaded: <desc> (<file>)
      const cached = line.match(/^\[\+\] Already downloaded: (.+?) \(/);
      if (cached) {
        started++;
        pushEvent(job, {
          phase: "downloading",
          percent: totalCount
            ? Math.round((started / totalCount) * 100)
            : undefined,
          bytes: started,
          total: totalCount,
          message: `Already installed (${started} of ${totalCount}): ${cached[1]}`,
          timestamp: Date.now(),
        });
        continue;
      }
    }
  });

  child.on("close", async (code) => {
    if (code !== 0) {
      pushEvent(job, {
        phase: "failed",
        error: `download-knowledge.sh exited ${code}: ${stderrTail.trim().slice(-400)}`,
        timestamp: Date.now(),
      });
      return;
    }

    // Post-validate: scan each member's file and report any that
    // failed to download cleanly. The bash script's --fail flag now
    // catches HTTP errors, but a network glitch can still leave a
    // truncated file behind, and an upstream catalog drift could
    // produce a 200 response with the wrong content. This is the
    // safety net that prevents another infinite "install missing" loop.
    if (memberKeys.length > 0) {
      try {
        const catalog = await loadCatalogForCleanup(REPO);
        const stillBad: string[] = [];
        for (const key of memberKeys) {
          const pkg = catalog.packages.find((p) => p.key === key);
          if (!pkg) continue;
          if (!pkg.installed) {
            stillBad.push(
              pkg.corrupt ? `${key} (${pkg.corrupt})` : `${key} (missing)`,
            );
          }
        }
        if (stillBad.length > 0) {
          pushEvent(job, {
            phase: "failed",
            error: `Bundle install completed but ${stillBad.length} package${stillBad.length === 1 ? "" : "s"} could not be downloaded: ${stillBad.join(", ")}. The catalog URL may be stale upstream.`,
            timestamp: Date.now(),
          });
          return;
        }
      } catch {
        /* fall through and continue with restart */
      }
    }

    pushEvent(job, {
      phase: "downloading",
      percent: 100,
      bytes: totalCount,
      total: totalCount,
      message: "All packages downloaded",
      timestamp: Date.now(),
    });

    // Bundles only exist for knowledge → always restart kiwix.
    if (isRestartable("krull-kiwix")) {
      pushEvent(job, {
        phase: "restarting",
        message: "Restarting krull-kiwix",
        timestamp: Date.now(),
      });
      try {
        await restartContainer("krull-kiwix");
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
      message: `Installed bundle ${bundleKey}`,
      timestamp: Date.now(),
    });
  });

  child.on("error", (err) => {
    pushEvent(job, {
      phase: "failed",
      error: `spawn failed: ${err.message}`,
      timestamp: Date.now(),
    });
  });
}

/**
 * After a download finishes, peek at the first few bytes of the file
 * and confirm it matches the expected magic for its kind. ZIM files
 * start with the four bytes "ZIM\x04"; PMTiles start with "PMTiles".
 * Anything else (HTML 404 pages from a stale mirror, partial
 * truncated downloads with no header at all) is rejected.
 */
async function validateDownload(
  filePath: string,
  pkg: CatalogPackage,
): Promise<{ ok: true } | { ok: false; reason: string }> {
  let head: Buffer;
  try {
    const fh = await fs.open(filePath, "r");
    try {
      const buf = Buffer.alloc(8);
      await fh.read(buf, 0, 8, 0);
      head = buf;
    } finally {
      await fh.close();
    }
  } catch (err) {
    return { ok: false, reason: `could not read downloaded file: ${(err as Error).message}` };
  }

  // HTML detection — if the response body starts with "<!DOCTYPE" or "<html",
  // the mirror returned an error page, not the file we asked for.
  const headStr = head.toString("utf8");
  if (/^\s*<(?:!doctype|html)/i.test(headStr)) {
    return {
      ok: false,
      reason: `download returned an HTML page instead of the expected file. The catalog entry for ${pkg.key} likely points at a stale URL upstream — please report it.`,
    };
  }

  if (pkg.kind === "knowledge" || pkg.kind === "wikipedia") {
    // ZIM file magic: "ZIM\x04" (5A 49 4D 04)
    if (head[0] !== 0x5a || head[1] !== 0x49 || head[2] !== 0x4d || head[3] !== 0x04) {
      return {
        ok: false,
        reason: `downloaded file is not a valid ZIM (got bytes ${[...head.slice(0, 4)].map((b) => b.toString(16).padStart(2, "0")).join(" ")})`,
      };
    }
  }

  if (pkg.kind === "maps") {
    // PMTiles magic: "PMTiles" (50 4D 54 69 6C 65 73)
    const expected = Buffer.from("PMTiles", "ascii");
    if (!head.slice(0, 7).equals(expected)) {
      return {
        ok: false,
        reason: `downloaded file is not a valid PMTiles archive (got bytes ${[...head.slice(0, 7)].map((b) => b.toString(16).padStart(2, "0")).join(" ")})`,
      };
    }
  }

  return { ok: true };
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
