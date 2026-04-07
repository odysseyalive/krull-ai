import Docker from "dockerode";

// Default to /var/run/docker.sock when inside the krull-home container
// (the socket is bind-mounted in docker-compose.yml).
export const docker = new Docker({ socketPath: "/var/run/docker.sock" });

export type ServiceState = "running" | "exited" | "restarting" | "missing" | "unknown";

export interface ServiceStatus {
  name: string;
  container: string;
  state: ServiceState;
  url: string;
}

const RESTARTABLE = new Set<string>([
  "krull-webui",
  "krull-kiwix",
  "krull-tileserver",
  "krull-map-viewer",
]);

export function isRestartable(container: string): boolean {
  return RESTARTABLE.has(container);
}

export async function getContainerState(name: string): Promise<ServiceState> {
  try {
    const container = docker.getContainer(name);
    const info = await container.inspect();
    const state = info.State.Status;
    if (state === "running") return "running";
    if (state === "restarting") return "restarting";
    if (state === "exited") return "exited";
    return "unknown";
  } catch (err) {
    const e = err as { statusCode?: number };
    if (e.statusCode === 404) return "missing";
    return "unknown";
  }
}

export async function restartContainer(name: string): Promise<void> {
  if (!isRestartable(name)) {
    throw new Error(`refusing to restart non-whitelisted container: ${name}`);
  }
  await docker.getContainer(name).restart({ t: 5 });
}
