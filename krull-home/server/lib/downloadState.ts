/**
 * Persistent download state + error log.
 *
 * This module is the bridge between the bash download scripts (which
 * write their manifest to data/downloads/state.json via
 * scripts/lib/download-log.sh) and the web UI (which polls
 * /api/library/downloads/state to render live progress and queue
 * position across page navigations).
 *
 * Design notes:
 *   - The state file stores only the manifest (list of target paths +
 *     expected byte sizes). Progress percentages are computed on read
 *     by stat-ing the files on disk. This means there is no PID to
 *     track and no IPC coupling: if the bash script dies, the file
 *     sizes simply stop advancing and the reader notices via the
 *     `updatedAt` field.
 *   - Writes are atomic (temp-file + rename) and guarded by an async
 *     mutex so the installer, the installQueue, and the bash scripts
 *     (which write the file directly via python3) don't trample each
 *     other.
 *   - Errors are append-only JSONL — never truncated. This file is a
 *     diagnostic tool: users grep it to find bad catalog URLs that
 *     need replacement or removal.
 */
import fs from "node:fs/promises";
import fsSync from "node:fs";
import path from "node:path";
import { randomUUID } from "node:crypto";

const REPO = process.env.KRULL_REPO ?? "/workspace";
const STATE_DIR = path.join(REPO, "data", "downloads");
const STATE_FILE = path.join(STATE_DIR, "state.json");
const ERRORS_FILE = path.join(STATE_DIR, "errors.jsonl");

export interface ManifestEntry {
  path: string;
  expectedBytes: number;
}

export interface ActiveDownload {
  jobId: string;
  kind: string;
  key: string;
  name: string;
  phase: "queued" | "downloading" | "restarting" | "done" | "failed";
  startedAt: number;
  updatedAt: number;
  manifest: ManifestEntry[];
  message?: string;
}

export interface QueuedDownload {
  jobId: string;
  kind: string;
  key: string;
  name: string;
  queuedAt: number;
}

export interface DownloadState {
  active: ActiveDownload | null;
  queue: QueuedDownload[];
}

export interface DownloadStateWithProgress {
  active:
    | (ActiveDownload & {
        bytes: number;
        total: number;
        percent: number | null;
      })
    | null;
  queue: QueuedDownload[];
}

export interface DownloadErrorEntry {
  timestamp: string;
  kind: string;
  key: string;
  file: string;
  url: string;
  httpStatus: number | null;
  curlExit: number | null;
  reason: string;
  source: string;
}

// ---- async mutex ----------------------------------------------------------
// A tiny chain-of-promises lock so writes never race. The bash scripts
// write directly via python3 (not through this mutex), so writes can
// still interleave across processes — but within krull-home, at least
// we're self-consistent. The atomic rename semantics of POSIX give us
// cross-process safety against torn reads.

let writeChain: Promise<void> = Promise.resolve();
function withLock<T>(fn: () => Promise<T>): Promise<T> {
  const next = writeChain.then(fn, fn);
  // Don't propagate rejections through the chain; each caller handles
  // its own errors.
  writeChain = next.then(
    () => undefined,
    () => undefined,
  );
  return next;
}

async function ensureDir(): Promise<void> {
  await fs.mkdir(STATE_DIR, { recursive: true });
}

async function atomicWriteJSON(filePath: string, doc: unknown): Promise<void> {
  await ensureDir();
  const tmp = path.join(
    path.dirname(filePath),
    `.${path.basename(filePath)}.${randomUUID()}.tmp`,
  );
  await fs.writeFile(tmp, JSON.stringify(doc));
  await fs.rename(tmp, filePath);
}

// ---- reads ---------------------------------------------------------------

/** Read the raw state document — no progress computation. */
export async function readState(): Promise<DownloadState> {
  try {
    const text = await fs.readFile(STATE_FILE, "utf8");
    const doc = JSON.parse(text) as Partial<DownloadState>;
    return {
      active: doc.active ?? null,
      queue: Array.isArray(doc.queue) ? doc.queue : [],
    };
  } catch (err) {
    if ((err as NodeJS.ErrnoException).code === "ENOENT") {
      return { active: null, queue: [] };
    }
    // JSON.parse or partial-write failures — behave as if empty rather
    // than crashing the endpoint.
    return { active: null, queue: [] };
  }
}

/**
 * Read the state and augment the active entry with computed progress
 * by stat-ing each manifest file. This is what the HTTP endpoint
 * returns on each poll.
 */
export async function readStateWithProgress(): Promise<DownloadStateWithProgress> {
  const state = await readState();
  if (!state.active) {
    return { active: null, queue: state.queue };
  }
  let bytes = 0;
  let total = 0;
  for (const entry of state.active.manifest) {
    total += entry.expectedBytes || 0;
    try {
      const st = await fs.stat(entry.path);
      bytes += st.size;
    } catch {
      // file not yet created — counts as 0 bytes
    }
  }
  let percent: number | null = null;
  if (total > 0) {
    // Cap at 99 during download so the bar only flips to 100 when the
    // script explicitly marks phase=done (matches installer.ts behavior).
    if (state.active.phase === "done") {
      percent = 100;
    } else {
      percent = Math.min(99, Math.round((bytes / total) * 100));
    }
  }
  return {
    active: { ...state.active, bytes, total, percent },
    queue: state.queue,
  };
}

// ---- writes --------------------------------------------------------------

/** Replace the active slot entirely. Pass `null` to clear. */
export async function setActive(active: ActiveDownload | null): Promise<void> {
  await withLock(async () => {
    const current = await readState();
    await atomicWriteJSON(STATE_FILE, { ...current, active });
  });
}

/** Clear the active slot. */
export async function clearActive(): Promise<void> {
  await setActive(null);
}

/** Replace the queue list atomically. */
export async function writeQueue(queue: QueuedDownload[]): Promise<void> {
  await withLock(async () => {
    const current = await readState();
    await atomicWriteJSON(STATE_FILE, { ...current, queue });
  });
}

/**
 * Update a few fields on the active entry (phase, message, updatedAt).
 * If there is no active entry, does nothing.
 */
export async function patchActive(
  patch: Partial<Pick<ActiveDownload, "phase" | "message" | "updatedAt">>,
): Promise<void> {
  await withLock(async () => {
    const current = await readState();
    if (!current.active) return;
    const updated: ActiveDownload = {
      ...current.active,
      ...patch,
      updatedAt: patch.updatedAt ?? Date.now(),
    };
    await atomicWriteJSON(STATE_FILE, { ...current, active: updated });
  });
}

// ---- errors log ----------------------------------------------------------

export async function appendError(
  entry: Omit<DownloadErrorEntry, "timestamp"> & { timestamp?: string },
): Promise<void> {
  await ensureDir();
  const line =
    JSON.stringify({
      timestamp: entry.timestamp ?? new Date().toISOString(),
      kind: entry.kind,
      key: entry.key,
      file: entry.file,
      url: entry.url,
      httpStatus: entry.httpStatus ?? null,
      curlExit: entry.curlExit ?? null,
      reason: entry.reason,
      source: entry.source,
    }) + "\n";
  // appendFile is atomic for writes < PIPE_BUF (4kB on Linux), which
  // our single-line entries always are. No need for the mutex here.
  await fs.appendFile(ERRORS_FILE, line);
}

/** Read the last `limit` error entries, newest first. */
export async function readErrors(limit = 200): Promise<DownloadErrorEntry[]> {
  try {
    const text = await fs.readFile(ERRORS_FILE, "utf8");
    const lines = text.split("\n").filter((l) => l.trim().length > 0);
    const tail = lines.slice(-limit).reverse();
    const out: DownloadErrorEntry[] = [];
    for (const line of tail) {
      try {
        out.push(JSON.parse(line) as DownloadErrorEntry);
      } catch {
        // skip malformed lines
      }
    }
    return out;
  } catch (err) {
    if ((err as NodeJS.ErrnoException).code === "ENOENT") return [];
    throw err;
  }
}

/** Synchronous version used only at krull-home startup for recovery. */
export function readStateSync(): DownloadState {
  try {
    const text = fsSync.readFileSync(STATE_FILE, "utf8");
    const doc = JSON.parse(text) as Partial<DownloadState>;
    return {
      active: doc.active ?? null,
      queue: Array.isArray(doc.queue) ? doc.queue : [],
    };
  } catch {
    return { active: null, queue: [] };
  }
}
