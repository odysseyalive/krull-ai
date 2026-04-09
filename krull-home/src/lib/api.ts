export type ServiceState =
  | "running"
  | "exited"
  | "restarting"
  | "missing"
  | "unknown";

export interface ServiceStatus {
  name: string;
  container: string;
  url: string;
  state: ServiceState;
}

export async function fetchServices(): Promise<ServiceStatus[]> {
  const res = await fetch("/api/services");
  if (!res.ok) throw new Error(`/api/services -> ${res.status}`);
  const data = (await res.json()) as { services: ServiceStatus[] };
  return data.services;
}

export type EnvFieldKind = "text" | "number" | "secret";
export interface EnvField {
  key: string;
  label: string;
  description: string;
  kind: EnvFieldKind;
  default?: string;
  group: string;
  affects: string[];
}
export interface EnvPayload {
  path: string;
  schema: EnvField[];
  values: Record<string, string>;
  extras: string[];
}

export async function fetchEnv(): Promise<EnvPayload> {
  const res = await fetch("/api/env");
  if (!res.ok) throw new Error(`/api/env -> ${res.status}`);
  return (await res.json()) as EnvPayload;
}

export async function saveEnv(
  values: Record<string, string>,
): Promise<{ ok: true; changed: string[]; affects: string[]; retuneJobId?: string }> {
  const res = await fetch("/api/env", {
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ values }),
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`/api/env -> ${res.status}: ${text}`);
  }
  return (await res.json()) as {
    ok: true;
    changed: string[];
    affects: string[];
    retuneJobId?: string;
  };
}

export type PackageKind = "knowledge" | "wikipedia" | "maps";
export interface CatalogPackage {
  kind: PackageKind;
  key: string;
  name: string;
  description: string;
  size: string;
  file: string;
  targetDir: string;
  category?: string;
  installed?: boolean;
  installedSizeBytes?: number;
  /** Set when a file exists on disk but failed integrity inspection. */
  corrupt?: string;
  /** Bytes on disk for a corrupt/partial file, used to render Resume %. */
  partialBytes?: number;
}
export interface CatalogBundle {
  kind: PackageKind;
  key: string;
  name: string;
  description: string;
  size: string;
  members: string[];
}
export interface Catalog {
  packages: CatalogPackage[];
  bundles: CatalogBundle[];
}

export async function fetchCatalog(): Promise<Catalog> {
  const res = await fetch("/api/library");
  if (!res.ok) throw new Error(`/api/library -> ${res.status}`);
  return (await res.json()) as Catalog;
}

export type JobPhase = "queued" | "downloading" | "restarting" | "done" | "failed";
export interface JobEvent {
  phase: JobPhase;
  percent?: number;
  bytes?: number;
  total?: number;
  message?: string;
  error?: string;
  timestamp: number;
  /** Bundle-only: which member (catalog key) this event pertains to. */
  memberKey?: string;
  /** Bundle-only: lifecycle state of the single member identified by memberKey. */
  memberStatus?: "downloading" | "installed" | "failed";
}

export async function startInstall(
  kind: PackageKind,
  key: string,
): Promise<{ jobId: string; position: number }> {
  const res = await fetch("/api/library/install", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ kind, key }),
  });
  if (!res.ok) throw new Error(`install failed: ${await res.text()}`);
  return (await res.json()) as { jobId: string; position: number };
}

export async function startBundleInstall(
  kind: PackageKind,
  key: string,
): Promise<{ jobId: string; position: number }> {
  const res = await fetch("/api/library/install-bundle", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ kind, key }),
  });
  if (!res.ok) throw new Error(`install bundle failed: ${await res.text()}`);
  return (await res.json()) as { jobId: string; position: number };
}

export async function deletePackage(
  kind: PackageKind,
  key: string,
): Promise<{ jobId: string; position: number }> {
  const res = await fetch("/api/library/delete", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ kind, key }),
  });
  if (!res.ok) throw new Error(`delete failed: ${await res.text()}`);
  return (await res.json()) as { jobId: string; position: number };
}

export interface ContextSuggestion {
  numCtx: number;
  compactLimit: number;
  rationale: string;
}
export interface RecommendedModel {
  key: string;
  label: string;
  vram: string;
  description: string;
  bestFor: string;
  installed: boolean;
  active: boolean;
  contextSuggestion?: ContextSuggestion;
}
export interface ModelsPayload {
  active: string;
  recommended: RecommendedModel[];
  other: string[];
}

export async function fetchModels(): Promise<ModelsPayload> {
  const res = await fetch("/api/models");
  if (!res.ok) throw new Error(`/api/models -> ${res.status}`);
  return (await res.json()) as ModelsPayload;
}

export interface HardwareRam {
  totalBytes: number;
  availableBytes: number;
}
export interface HardwareGpu {
  vendor: "nvidia" | "none";
  name?: string;
  totalBytes?: number;
  freeBytes?: number;
}
export interface Hardware {
  ram: HardwareRam;
  gpu: HardwareGpu;
}

export async function fetchHardware(): Promise<{ hardware: Hardware }> {
  const res = await fetch("/api/system/hardware");
  if (!res.ok) throw new Error(`/api/system/hardware -> ${res.status}`);
  return (await res.json()) as { hardware: Hardware };
}

export async function deleteModel(key: string): Promise<{ ok: true; deleted: string }> {
  const res = await fetch(`/api/models/${encodeURIComponent(key)}`, {
    method: "DELETE",
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`delete failed (${res.status}): ${text}`);
  }
  return (await res.json()) as { ok: true; deleted: string };
}

export async function pullModel(key: string): Promise<{ jobId: string }> {
  const res = await fetch("/api/models/install", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ key }),
  });
  if (!res.ok) throw new Error(`pull failed: ${await res.text()}`);
  return (await res.json()) as { jobId: string };
}

export async function selectModel(key: string): Promise<void> {
  const res = await fetch("/api/models/select", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ key }),
  });
  if (!res.ok) throw new Error(`select failed: ${await res.text()}`);
}

export interface VersionInfo {
  commit: string;
  shortCommit: string;
  branch: string | null;
}
export type UpdatePhase = "idle" | "running" | "complete" | "failed";
export interface UpdateStatus {
  phase: UpdatePhase;
  timestamp?: string;
  message?: string;
}

export async function fetchVersion(signal?: AbortSignal): Promise<VersionInfo> {
  const res = await fetch("/api/version", { signal });
  if (!res.ok) throw new Error(`/api/version -> ${res.status}`);
  return (await res.json()) as VersionInfo;
}

export async function triggerUpdate(): Promise<{ requestedAt: string }> {
  const res = await fetch("/api/update", { method: "POST" });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`/api/update -> ${res.status}: ${text}`);
  }
  return (await res.json()) as { ok: true; requestedAt: string };
}

export async function fetchUpdateStatus(signal?: AbortSignal): Promise<UpdateStatus> {
  const res = await fetch("/api/update/status", { signal });
  if (!res.ok) throw new Error(`/api/update/status -> ${res.status}`);
  return (await res.json()) as UpdateStatus;
}

export interface DownloadManifestEntry {
  path: string;
  expectedBytes: number;
}
export interface ActiveDownload {
  jobId: string;
  kind: string;
  key: string;
  name: string;
  phase: JobPhase;
  startedAt: number;
  updatedAt: number;
  manifest: DownloadManifestEntry[];
  bytes: number;
  total: number;
  percent: number | null;
  message?: string;
}
export interface QueuedDownload {
  jobId: string;
  kind: string;
  key: string;
  name: string;
  queuedAt: number;
}
export interface DownloadStateSnapshot {
  active: ActiveDownload | null;
  queue: QueuedDownload[];
}

export async function fetchDownloadState(): Promise<DownloadStateSnapshot> {
  const res = await fetch("/api/library/downloads/state");
  if (!res.ok) throw new Error(`/api/library/downloads/state -> ${res.status}`);
  return (await res.json()) as DownloadStateSnapshot;
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

export async function fetchDownloadErrors(
  limit = 200,
): Promise<DownloadErrorEntry[]> {
  const res = await fetch(`/api/library/downloads/errors?limit=${limit}`);
  if (!res.ok) throw new Error(`/api/library/downloads/errors -> ${res.status}`);
  const data = (await res.json()) as { entries: DownloadErrorEntry[] };
  return data.entries;
}

export function streamJob(jobId: string, onEvent: (ev: JobEvent) => void): () => void {
  const es = new EventSource(`/api/jobs/${encodeURIComponent(jobId)}/stream`);
  es.onmessage = (msg) => {
    try {
      onEvent(JSON.parse(msg.data) as JobEvent);
    } catch {
      /* ignore parse errors */
    }
  };
  es.onerror = () => {
    // EventSource auto-retries — close on terminal phase from caller side.
  };
  return () => es.close();
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let val = bytes / 1024;
  let i = 0;
  while (val >= 1024 && i < units.length - 1) {
    val /= 1024;
    i++;
  }
  return `${val.toFixed(val < 10 ? 1 : 0)} ${units[i]}`;
}

export async function restartContainer(
  name: string,
): Promise<{ ok: true; container: string }> {
  const res = await fetch(`/api/restart/${encodeURIComponent(name)}`, {
    method: "POST",
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`/api/restart -> ${res.status}: ${text}`);
  }
  return (await res.json()) as { ok: true; container: string };
}
