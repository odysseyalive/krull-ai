/**
 * Host hardware detection: installed RAM + GPU VRAM.
 *
 * Used by `/api/system/hardware` to drive per-model context-window
 * recommendations on the settings page.
 *
 * Design notes:
 *   - RAM: /proc/meminfo inside the krull-home container sees the
 *     host kernel's values directly (no namespace isolation for
 *     memory on bind-mounted /proc). MemAvailable is the correct
 *     field to budget against — it includes page cache we could
 *     evict if ollama needs it.
 *   - GPU: we shell out to `docker exec krull-ollama nvidia-smi …`
 *     rather than expecting nvidia-smi inside krull-home. The ollama
 *     sibling container is the one with the nvidia runtime, so it's
 *     the only place nvidia-smi exists. krull-home already has the
 *     docker CLI + socket access for restarting siblings, so this
 *     piggybacks on existing infrastructure.
 *   - Cache: probes take ~50ms on a cold run — cheap enough that
 *     a 10-second cache is plenty. The UI polls at most once a
 *     page load; this is belt-and-braces against accidental polling.
 */
import fs from "node:fs/promises";
import { spawn } from "node:child_process";

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

let cached: { at: number; value: Hardware } | null = null;
const CACHE_TTL_MS = 10_000;

export async function detectHardware(): Promise<Hardware> {
  const now = Date.now();
  if (cached && now - cached.at < CACHE_TTL_MS) {
    return cached.value;
  }
  const [ram, gpu] = await Promise.all([detectRam(), detectGpu()]);
  const value: Hardware = { ram, gpu };
  cached = { at: now, value };
  return value;
}

async function detectRam(): Promise<HardwareRam> {
  try {
    const text = await fs.readFile("/proc/meminfo", "utf8");
    const parse = (name: string): number => {
      const m = text.match(new RegExp(`^${name}:\\s+(\\d+)\\s+kB`, "m"));
      return m ? parseInt(m[1], 10) * 1024 : 0;
    };
    return {
      totalBytes: parse("MemTotal"),
      availableBytes: parse("MemAvailable"),
    };
  } catch {
    return { totalBytes: 0, availableBytes: 0 };
  }
}

async function detectGpu(): Promise<HardwareGpu> {
  // nvidia-smi lives in the krull-ollama container (the one with the
  // nvidia runtime). We reach it via `docker exec` from here.
  const stdout = await run("docker", [
    "exec",
    "krull-ollama",
    "nvidia-smi",
    "--query-gpu=name,memory.total,memory.free",
    "--format=csv,noheader,nounits",
  ]);
  if (!stdout) return { vendor: "none" };

  const first = stdout
    .split("\n")
    .map((l) => l.trim())
    .filter((l) => l.length > 0)[0];
  if (!first) return { vendor: "none" };

  const parts = first.split(",").map((s) => s.trim());
  if (parts.length < 3) return { vendor: "none" };

  const name = parts[0];
  const totalMb = parseInt(parts[1], 10);
  const freeMb = parseInt(parts[2], 10);
  if (!Number.isFinite(totalMb) || !Number.isFinite(freeMb)) {
    return { vendor: "none" };
  }
  return {
    vendor: "nvidia",
    name,
    totalBytes: totalMb * 1024 * 1024,
    freeBytes: freeMb * 1024 * 1024,
  };
}

/** Run a command, return stdout on exit 0, empty string otherwise. */
function run(cmd: string, args: string[], timeoutMs = 3000): Promise<string> {
  return new Promise((resolve) => {
    const child = spawn(cmd, args, { stdio: ["ignore", "pipe", "pipe"] });
    let stdout = "";
    let settled = false;
    const timer = setTimeout(() => {
      if (!settled) {
        settled = true;
        child.kill("SIGKILL");
        resolve("");
      }
    }, timeoutMs);
    child.stdout.on("data", (chunk: Buffer) => {
      stdout += chunk.toString();
    });
    child.on("error", () => {
      if (!settled) {
        settled = true;
        clearTimeout(timer);
        resolve("");
      }
    });
    child.on("close", (code) => {
      if (!settled) {
        settled = true;
        clearTimeout(timer);
        resolve(code === 0 ? stdout : "");
      }
    });
  });
}
