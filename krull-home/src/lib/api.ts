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
): Promise<{ ok: true; changed: string[]; affects: string[] }> {
  const res = await fetch("/api/env", {
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ values }),
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`/api/env -> ${res.status}: ${text}`);
  }
  return (await res.json()) as { ok: true; changed: string[]; affects: string[] };
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
}

export async function startInstall(
  kind: PackageKind,
  key: string,
): Promise<{ jobId: string }> {
  const res = await fetch("/api/library/install", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ kind, key }),
  });
  if (!res.ok) throw new Error(`install failed: ${await res.text()}`);
  return (await res.json()) as { jobId: string };
}

export async function deletePackage(
  kind: PackageKind,
  key: string,
): Promise<void> {
  const res = await fetch("/api/library/delete", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ kind, key }),
  });
  if (!res.ok) throw new Error(`delete failed: ${await res.text()}`);
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
