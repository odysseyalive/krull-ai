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
  // litellm reads its config.yaml (a bind-mounted file) at startup, so a
  // plain restart re-reads it. selectActiveModel patches that file and needs
  // this restart to actually take effect.
  "krull-litellm",
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

// Containers whose environment is derived from .env (via compose ${VAR}
// interpolation) and therefore must be RECREATED — not merely restarted — to
// pick up a changed value. A plain `docker restart` reuses the container's
// create-time environment, so it never sees the new value.
const RECREATABLE = new Set<string>([
  "krull-sse-proxy",
  "krull-ollama",
  "krull-litellm",
  "krull-webui",
  "krull-photon",
  "krull-tileserver",
]);

export function isRecreatable(container: string): boolean {
  return RECREATABLE.has(container);
}

/**
 * Recreate a container in place with a set of environment overrides applied,
 * preserving everything else about it (image, command, binds, ports,
 * networks, labels, restart policy). This is how a settings save actually
 * takes effect: compose bakes `${VAR}` values into the container at
 * create-time, so the only way to apply a new value is to recreate.
 *
 * We deliberately do NOT shell out to `docker compose`:
 *   1. The compose plugin isn't installed in the krull-home image.
 *   2. Running compose from inside a container would resolve the compose
 *      file's relative bind paths against the container's filesystem, then
 *      hand those (wrong) paths to the host daemon — silently detaching the
 *      real data volumes. Reusing the inspected HostConfig keeps the
 *      host-resolved absolute bind paths exactly as compose first set them.
 *
 * `overrides` is keyed by the container's OWN environment variable name
 * (e.g. OLLAMA_CONTEXT_LENGTH for krull-ollama), which is not always the
 * same as the .env key the user edited — the caller resolves that mapping.
 *
 * Safe swap: stop → rename the old container aside → create + start the new
 * one → remove the old. On any failure the old container is renamed back and
 * restarted, so a botched recreate never leaves the service missing.
 */
export async function recreateContainerWithEnv(
  name: string,
  overrides: Record<string, string>,
): Promise<void> {
  if (!isRecreatable(name)) {
    throw new Error(`refusing to recreate non-whitelisted container: ${name}`);
  }
  const old = docker.getContainer(name);
  const info = await old.inspect();

  // Apply overrides to the existing Env array: replace matching keys in
  // place, append any that weren't already present.
  const seen = new Set<string>();
  const newEnv = (info.Config.Env ?? []).map((entry) => {
    const eq = entry.indexOf("=");
    const key = eq >= 0 ? entry.slice(0, eq) : entry;
    if (Object.prototype.hasOwnProperty.call(overrides, key)) {
      seen.add(key);
      return `${key}=${overrides[key]}`;
    }
    return entry;
  });
  for (const [k, v] of Object.entries(overrides)) {
    if (!seen.has(k)) newEnv.push(`${k}=${v}`);
  }

  const networks = info.NetworkSettings?.Networks ?? {};
  const netNames = Object.keys(networks);
  const primaryNet = netNames[0];
  const aliasesFor = (net: { Aliases?: string[] | null }): string[] =>
    (net.Aliases ?? []).filter((a) => a && !info.Id.startsWith(a));

  // Build create options from the inspected config. Spreading Config
  // preserves image, cmd, entrypoint, working dir, exposed ports, labels
  // (including the compose ownership labels), tty, etc.
  const createOpts: Record<string, unknown> = {
    ...info.Config,
    name,
    Env: newEnv,
    HostConfig: info.HostConfig,
  };
  // Let Docker assign a fresh hostname rather than reusing the old
  // container's short-ID hostname baked into info.Config.
  delete createOpts.Hostname;
  if (primaryNet) {
    createOpts.NetworkingConfig = {
      EndpointsConfig: {
        [primaryNet]: { Aliases: aliasesFor(networks[primaryNet]) },
      },
    };
  }

  const wasRunning = info.State?.Running === true;
  const parked = `${name}__old`;
  // Bind by immutable ID so stop/start still work after the rename below.
  const byId = docker.getContainer(info.Id);

  // Clear any leftover parked container from a prior failed recreate. If one
  // exists and can't be removed, ABORT before touching the live container —
  // otherwise the rename below fails with the service already renamed/stopped.
  try {
    await docker.getContainer(parked).remove({ force: true });
  } catch (err) {
    if ((err as { statusCode?: number }).statusCode !== 404) {
      throw new Error(
        `refusing to recreate ${name}: leftover ${parked} present and not removable: ${(err as Error).message}`,
      );
    }
  }

  // Rename the (still-running) old container aside FIRST. Renaming does not
  // stop it, so if this throws the running service is left untouched. Only
  // after a successful rename do we stop it — which means a rename failure can
  // never leave the service down.
  await old.rename({ name: parked });

  const rollback = async (): Promise<void> => {
    try {
      await docker.getContainer(parked).rename({ name });
      if (wasRunning) await byId.start();
    } catch {
      /* best-effort rollback */
    }
  };

  let created: Awaited<ReturnType<typeof docker.createContainer>>;
  try {
    // Stop the parked container to free its published ports before the new
    // one binds them.
    if (wasRunning) {
      try {
        await byId.stop({ t: 10 });
      } catch {
        /* already stopped */
      }
    }
    created = await docker.createContainer(createOpts as Parameters<typeof docker.createContainer>[0]);
  } catch (err) {
    await rollback();
    throw new Error(`recreate of ${name} failed at create: ${(err as Error).message}`);
  }

  try {
    // Reconnect any networks beyond the primary (with their aliases).
    for (const netName of netNames.slice(1)) {
      const net = networks[netName];
      try {
        await docker
          .getNetwork(net.NetworkID ?? netName)
          .connect({ Container: created.id, EndpointConfig: { Aliases: aliasesFor(net) } });
      } catch {
        /* best-effort */
      }
    }
    await created.start();
  } catch (err) {
    try {
      await created.remove({ force: true });
    } catch {
      /* ignore */
    }
    await rollback();
    throw new Error(`recreate of ${name} failed at start: ${(err as Error).message}`);
  }

  // Success — drop the parked old container.
  try {
    await docker.getContainer(parked).remove({ force: true });
  } catch {
    /* ignore */
  }
}
