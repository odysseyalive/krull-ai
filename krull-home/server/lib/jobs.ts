/**
 * In-memory job registry with SSE-friendly subscription. Each job has a
 * stream of events that subscribers can listen to. The full event log is
 * also retained so a late subscriber can replay everything since the
 * job started.
 */
import { EventEmitter } from "node:events";
import { randomUUID } from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { clearActive, patchActive } from "./downloadState.js";

export type JobPhase =
  | "queued"
  | "downloading"
  | "restarting"
  | "done"
  | "failed";

export interface JobEvent {
  phase: JobPhase;
  percent?: number;
  bytes?: number;
  total?: number;
  message?: string;
  error?: string;
  timestamp: number;
}

export interface Job {
  id: string;
  kind: string;
  key: string;
  createdAt: number;
  events: JobEvent[];
  phase: JobPhase;
  emitter: EventEmitter;
}

const jobs = new Map<string, Job>();

export function createJob(kind: string, key: string): Job {
  const job: Job = {
    id: randomUUID(),
    kind,
    key,
    createdAt: Date.now(),
    events: [],
    phase: "queued",
    emitter: new EventEmitter(),
  };
  // Allow many SSE subscribers without warnings.
  job.emitter.setMaxListeners(50);
  jobs.set(job.id, job);
  pushEvent(job, { phase: "queued", timestamp: Date.now() });
  return job;
}

export function getJob(id: string): Job | undefined {
  return jobs.get(id);
}

export function pushEvent(job: Job, event: JobEvent): void {
  job.events.push(event);
  job.phase = event.phase;
  job.emitter.emit("event", event);

  // Mirror phase/message into the persistent download-state so the
  // library page sees "Restarting…" / "Failed" / "Installed" even when
  // it wasn't watching the SSE stream. Fire-and-forget; never block
  // the event loop on a disk write.
  if (
    event.phase === "downloading" ||
    event.phase === "restarting"
  ) {
    void patchActive({
      phase: event.phase,
      message: event.message,
    }).catch(() => {});
  }

  // Persist failures and completions to data/.install-log so the user
  // can grep for "which package failed?" after the in-memory job has
  // been GC'd or krull-home has been restarted. Successes are logged
  // too because they're useful breadcrumbs for "what did I install
  // last week?"
  if (event.phase === "failed" || event.phase === "done") {
    void appendInstallLog(job, event);
    // Clear the active slot on terminal events. The bash script's
    // EXIT trap also calls dl_state_end, but the two writes are
    // idempotent and the one that lands last wins cleanly.
    void clearActive().catch(() => {});
  }
}

const REPO = process.env.KRULL_REPO ?? "/workspace";
const INSTALL_LOG = path.join(REPO, "data", ".install-log");

async function appendInstallLog(job: Job, event: JobEvent): Promise<void> {
  try {
    const ts = new Date(event.timestamp).toISOString();
    const status = event.phase === "done" ? "OK  " : "FAIL";
    const detail =
      event.phase === "failed"
        ? event.error ?? event.message ?? ""
        : event.message ?? "";
    const line = `${ts} ${status} ${job.kind}/${job.key} — ${detail}\n`;
    // Use sync mkdir + appendFile to keep error handling simple — this
    // is a write-and-forget side effect, not a critical path.
    fs.mkdirSync(path.dirname(INSTALL_LOG), { recursive: true });
    fs.appendFileSync(INSTALL_LOG, line);
  } catch {
    /* never fail the install because we couldn't write a log line */
  }
}

/** Drop jobs that finished more than `ageMs` ago to prevent memory growth. */
export function gcJobs(ageMs = 60 * 60 * 1000): void {
  const now = Date.now();
  for (const [id, job] of jobs) {
    if ((job.phase === "done" || job.phase === "failed") && now - job.createdAt > ageMs) {
      jobs.delete(id);
    }
  }
}

setInterval(() => gcJobs(), 5 * 60 * 1000).unref();

/**
 * Wait until a job reaches a terminal phase (done | failed). Resolves
 * regardless of outcome — the queue uses this to know when one runner
 * is finished so it can move to the next.
 */
export function awaitJobTerminal(job: Job): Promise<void> {
  return new Promise((resolve) => {
    if (job.phase === "done" || job.phase === "failed") {
      resolve();
      return;
    }
    const onEvent = (ev: JobEvent) => {
      if (ev.phase === "done" || ev.phase === "failed") {
        job.emitter.off("event", onEvent);
        resolve();
      }
    };
    job.emitter.on("event", onEvent);
  });
}
