/**
 * In-memory job registry with SSE-friendly subscription. Each job has a
 * stream of events that subscribers can listen to. The full event log is
 * also retained so a late subscriber can replay everything since the
 * job started.
 */
import { EventEmitter } from "node:events";
import { randomUUID } from "node:crypto";

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
