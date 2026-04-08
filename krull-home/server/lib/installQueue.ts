/**
 * Single-worker FIFO queue for library install / delete / bundle-install
 * jobs. Without this, multiple click-installs would race each other:
 *   - parallel curl downloads competing for bandwidth
 *   - multiple kiwix restarts churning the container
 *   - download-knowledge.sh writing /tmp/Modelfile concurrently
 *
 * The queue solves all of that by serializing operations through one
 * worker. Each enqueued runner is responsible for emitting its own
 * progress events through the Job system; the queue just observes the
 * job's terminal phase to know when to move on.
 *
 * The queue lives in-process. If krull-home restarts, the queue is
 * lost — but no data is lost (the on-disk install state is the source
 * of truth and the user can re-click).
 */
import { type Job, pushEvent, awaitJobTerminal, getJob } from "./jobs.js";

interface QueueItem {
  jobId: string;
  label: string;
  runner: () => void | Promise<void>;
}

class InstallQueue {
  private queue: QueueItem[] = [];
  private currentJobId: string | null = null;

  /**
   * Add a runner to the queue. Returns the position the job was queued
   * at (0 = will run immediately). The job is expected to start in the
   * "queued" phase; the queue worker will let the runner emit its own
   * downloading/done/failed events through pushEvent().
   */
  enqueue(job: Job, label: string, runner: () => void | Promise<void>): number {
    const position = this.queue.length + (this.currentJobId ? 1 : 0);
    this.queue.push({ jobId: job.id, label, runner });

    // Tell the job (and any subscribers) where it sits in line.
    pushEvent(job, {
      phase: "queued",
      message:
        position === 0
          ? "Starting…"
          : position === 1
            ? "Up next…"
            : `Queued (#${position + 1})`,
      timestamp: Date.now(),
    });

    // Kick the worker if it's idle. We don't await this — the HTTP
    // route returns immediately and progress flows through the job
    // event stream.
    void this.tick();
    return position;
  }

  /** Number of jobs ahead of `jobId` in the queue (excluding the running one). */
  positionOf(jobId: string): number {
    if (this.currentJobId === jobId) return 0;
    const idx = this.queue.findIndex((it) => it.jobId === jobId);
    return idx === -1 ? -1 : idx + (this.currentJobId ? 1 : 0);
  }

  /** Total number of jobs in the queue + currently running. */
  size(): number {
    return this.queue.length + (this.currentJobId ? 1 : 0);
  }

  /** Snapshot of the queue for the /api/library/queue endpoint. */
  snapshot(): { running: string | null; pending: Array<{ jobId: string; label: string }> } {
    return {
      running: this.currentJobId,
      pending: this.queue.map((it) => ({ jobId: it.jobId, label: it.label })),
    };
  }

  private async tick(): Promise<void> {
    if (this.currentJobId) return; // worker already busy
    const next = this.queue.shift();
    if (!next) return;

    const job = getJob(next.jobId);
    if (!job) {
      // Job was GC'd before its turn — skip it and move on.
      void this.tick();
      return;
    }

    this.currentJobId = next.jobId;
    // Bump everyone behind us — they each move up one slot.
    this.broadcastPositions();

    try {
      // The runner kicks off the actual work and may return synchronously
      // (for spawn-and-forget runners). We then wait for the job to reach
      // a terminal phase via its event emitter.
      const ret = next.runner();
      if (ret && typeof (ret as Promise<void>).then === "function") {
        await ret;
      }
      await awaitJobTerminal(job);
    } catch {
      // If the runner throws synchronously the job won't have a
      // terminal event — emit one so subscribers don't hang.
      pushEvent(job, {
        phase: "failed",
        error: "runner threw before emitting events",
        timestamp: Date.now(),
      });
    }

    this.currentJobId = null;
    this.broadcastPositions();
    void this.tick();
  }

  private broadcastPositions(): void {
    for (let i = 0; i < this.queue.length; i++) {
      const item = this.queue[i];
      const job = getJob(item.jobId);
      if (!job || job.phase !== "queued") continue;
      const newPosition = i + (this.currentJobId ? 1 : 0);
      pushEvent(job, {
        phase: "queued",
        message:
          newPosition === 0
            ? "Starting…"
            : newPosition === 1
              ? "Up next…"
              : `Queued (#${newPosition + 1})`,
        timestamp: Date.now(),
      });
    }
  }
}

export const installQueue = new InstallQueue();
