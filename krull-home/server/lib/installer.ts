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
import { readStateWithProgress, appendError } from "./downloadState.js";

/**
 * Files that are unrecoverable-corrupt — there's no way to fix them
 * short of deleting and re-fetching. HTML 404 pages and wrong-magic
 * files are the canonical examples: curl -C - would resume garbage.
 *
 * "truncated" is DELIBERATELY NOT in this set: curl -C - resumes
 * truncated files cleanly, and the script's HEAD-probe (see
 * _dl_probe_and_reconcile in download-log.sh) will wipe the partial
 * if the upstream has drifted. Deleting truncated files blindly
 * would force a full re-download from zero and waste hours of
 * bandwidth on multi-GB ZIMs.
 */
const UNRECOVERABLE_CORRUPT = new Set(["html", "magic", "unreadable"]);

/**
 * Delete on-disk files for the given package keys IFF they exist and
 * their current catalog entry flags them as unrecoverable-corrupt.
 * Returns the list of cleaned keys (with reason tag) so the caller can
 * surface it in the job event stream.
 */
async function preCleanCorrupt(keys: string[]): Promise<string[]> {
  const cleaned: string[] = [];
  try {
    const catalog = await loadCatalogForCleanup(REPO);
    for (const key of keys) {
      const pkg = catalog.packages.find((p) => p.key === key);
      if (!pkg) continue;
      if (!pkg.corrupt || !UNRECOVERABLE_CORRUPT.has(pkg.corrupt)) continue;
      const filePath = path.join(REPO, pkg.targetDir, pkg.file);
      await fs.unlink(filePath).catch(() => {});
      // Also wipe the HEAD-probe sidecar so the next run re-probes
      // from scratch instead of trusting a stale ETag.
      await fs.unlink(`${filePath}.meta`).catch(() => {});
      cleaned.push(`${key} (${pkg.corrupt})`);
    }
  } catch {
    /* Catalog scan failed — proceed without pre-clean. */
  }
  return cleaned;
}

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

export async function startInstall(job: Job, pkg: CatalogPackage): Promise<void> {
  const script = scriptFor(pkg.kind);
  const target = path.join(REPO, pkg.targetDir, pkg.file);

  // Pre-clean any unrecoverable-corrupt leftovers for this single
  // package before spawning the script, mirroring the bundle path's
  // behavior. This lets a user click "Redownload" on a package whose
  // previous attempt left behind an HTML 404 page and actually get a
  // clean fetch instead of re-tripping the shortcircuit.
  const cleaned = await preCleanCorrupt([pkg.key]);
  if (cleaned.length > 0) {
    pushEvent(job, {
      phase: "downloading",
      percent: 0,
      bytes: 0,
      total: parseSize(pkg.size) ?? 0,
      message: `Cleaned corrupt file: ${cleaned.join(", ")}`,
      timestamp: Date.now(),
    });
  }

  pushEvent(job, {
    phase: "downloading",
    percent: 0,
    bytes: 0,
    total: parseSize(pkg.size) ?? 0,
    timestamp: Date.now(),
  });

  const child = spawn("bash", [script, pkg.key], {
    cwd: REPO,
    env: {
      ...process.env,
      KRULL_REPO: REPO,
      KRULL_DOWNLOAD_JOB_ID: job.id,
      KRULL_DOWNLOAD_KIND: pkg.kind,
    },
    stdio: ["ignore", "pipe", "pipe"],
  });

  let stderrTail = "";
  child.stderr.on("data", (chunk: Buffer) => {
    stderrTail = (stderrTail + chunk.toString()).slice(-2000);
  });
  child.stdout.on("data", () => {
    /* swallow — progress flows through downloadState.readStateWithProgress */
  });

  // Poll the download-state file the script is writing. This gives us
  // correct multi-file progress for free (maps + bundles) and does not
  // assume a single target path. If the state file hasn't been written
  // yet (script still starting up) we simply emit 0%.
  const poll = setInterval(async () => {
    try {
      const snap = await readStateWithProgress();
      if (snap.active && snap.active.jobId === job.id) {
        pushEvent(job, {
          phase: "downloading",
          percent: snap.active.percent ?? undefined,
          bytes: snap.active.bytes,
          total: snap.active.total,
          timestamp: Date.now(),
        });
      }
    } catch {
      /* ignore — state file not yet present */
    }
  }, 600);

  child.on("close", async (code) => {
    clearInterval(poll);
    if (code !== 0) {
      const errMsg = `script exited ${code}: ${stderrTail.trim().slice(-400)}`;
      await appendError({
        kind: pkg.kind,
        key: pkg.key,
        file: pkg.file,
        url: "",
        httpStatus: null,
        curlExit: code,
        reason: errMsg,
        source: "installer",
      }).catch(() => {});
      pushEvent(job, {
        phase: "failed",
        error: errMsg,
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
        await appendError({
          kind: pkg.kind,
          key: pkg.key,
          file: pkg.file,
          url: "",
          httpStatus: null,
          curlExit: null,
          reason: validation.reason,
          source: "installer-validation",
        }).catch(() => {});
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

  // Load the catalog once to get the full member details (file path,
  // expected byte size). We need these for both pre-clean (detecting
  // corrupt files) AND for intra-package progress polling during the
  // install — so the progress bar moves while a single large member is
  // actively downloading instead of sitting at 0% for 30 minutes.
  interface MemberInfo {
    key: string;
    name: string;
    filePath: string;
    expectedBytes: number;
  }
  const members: MemberInfo[] = [];
  try {
    const catalog = await loadCatalogForCleanup(REPO);
    for (const key of memberKeys) {
      const pkg = catalog.packages.find((p) => p.key === key);
      if (!pkg) continue;
      const filePath = path.join(REPO, pkg.targetDir, pkg.file);
      members.push({
        key: pkg.key,
        name: pkg.name,
        filePath,
        expectedBytes: parseSize(pkg.size) ?? 0,
      });
    }
  } catch {
    /* If catalog scan fails we proceed without progress polling */
  }

  // Pre-clean unrecoverable-corrupt files BEFORE running the script.
  // The shared helper handles the "why" — see preCleanCorrupt + the
  // UNRECOVERABLE_CORRUPT set comment. Truncated files are preserved
  // so curl -C - can resume them; the HEAD-probe in dl_run_curl will
  // separately detect upstream drift and wipe stale partials.
  const cleaned = await preCleanCorrupt(memberKeys);
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

  const child = spawn("bash", ["scripts/download-knowledge.sh", bundleKey], {
    cwd: REPO,
    env: {
      ...process.env,
      KRULL_REPO: REPO,
      KRULL_DOWNLOAD_JOB_ID: job.id,
      KRULL_DOWNLOAD_KIND: "knowledge",
    },
    stdio: ["ignore", "pipe", "pipe"],
  });

  let stderrTail = "";
  let stdoutBuf = "";
  // Track how many distinct packages have started so we can emit
  // "Installing X of N: <desc>" updates as the bash script chews
  // through the bundle. The progress bar shows CURRENT FILE progress
  // (0-100% per file, resetting for each member) so the user sees
  // continuous motion even when a single file is 20 GB. The label
  // always says "X of N" so they still know their position in the
  // bundle. For bundles with many tiny files, the bar will reset
  // repeatedly — that's fine because the label carries the overall
  // position.
  let currentIndex = -1;
  let currentLabel = "";
  let filePoll: NodeJS.Timeout | null = null;

  function stopPolling(): void {
    if (filePoll) {
      clearInterval(filePoll);
      filePoll = null;
    }
  }

  function startPolling(): void {
    stopPolling();
    const member = members[currentIndex];
    if (!member || member.expectedBytes <= 0) return;
    filePoll = setInterval(async () => {
      try {
        const st = await fs.stat(member.filePath);
        const percent = Math.min(
          99,
          Math.round((st.size / member.expectedBytes) * 100),
        );
        pushEvent(job, {
          phase: "downloading",
          percent,
          bytes: currentIndex,
          total: totalCount,
          message: `Installing ${currentIndex + 1} of ${totalCount}: ${currentLabel}`,
          timestamp: Date.now(),
        });
      } catch {
        /* file not created yet — curl hasn't opened it */
      }
    }, 1000);
  }

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
        stopPolling();
        currentIndex++;
        currentLabel = downloading[1];
        // Reset the bar to 0% for this new file — it'll fill as the
        // download progresses via the polling ticks below.
        pushEvent(job, {
          phase: "downloading",
          percent: 0,
          bytes: currentIndex,
          total: totalCount,
          message: `Installing ${currentIndex + 1} of ${totalCount}: ${currentLabel}`,
          timestamp: Date.now(),
        });
        startPolling();
        continue;
      }

      // Lines like: [+] Already downloaded: <desc> (<file>)
      const cached = line.match(/^\[\+\] Already downloaded: (.+?) \(/);
      if (cached) {
        stopPolling();
        currentIndex++;
        currentLabel = cached[1];
        // Cached members flash the bar to 100% briefly and move on.
        pushEvent(job, {
          phase: "downloading",
          percent: 100,
          bytes: currentIndex + 1,
          total: totalCount,
          message: `Already installed (${currentIndex + 1} of ${totalCount}): ${currentLabel}`,
          timestamp: Date.now(),
        });
        continue;
      }

      // Lines like: [+] Done: <filename>
      const done = line.match(/^\[\+\] Done: /);
      if (done) {
        // The current file just finished — flip the bar to 100% so the
        // user sees it complete before the next file resets it to 0%.
        stopPolling();
        pushEvent(job, {
          phase: "downloading",
          percent: 100,
          bytes: currentIndex + 1,
          total: totalCount,
          message: `Installing ${currentIndex + 1} of ${totalCount}: ${currentLabel}`,
          timestamp: Date.now(),
        });
        continue;
      }
    }
  });

  child.on("close", async (code) => {
    stopPolling();
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
    stopPolling();
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
