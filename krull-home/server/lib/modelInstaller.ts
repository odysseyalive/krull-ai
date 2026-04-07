/**
 * Pull an Ollama model via the existing scripts/pull-model.sh, streaming
 * job events through the same job/SSE infrastructure used by library
 * installs. Selecting a model also patches litellm/config.yaml and
 * restarts krull-litellm so the new active model is honoured by Claude Code.
 */
import { spawn } from "node:child_process";
import fs from "node:fs/promises";
import path from "node:path";
import { restartContainer, isRestartable } from "./docker.js";
import { type Job, pushEvent } from "./jobs.js";
import { readEnvFile, setValue, writeEnvFile } from "./envFile.js";

const REPO = process.env.KRULL_REPO ?? "/workspace";

export function startModelPull(job: Job, modelKey: string): void {
  pushEvent(job, {
    phase: "downloading",
    message: `Pulling ${modelKey}`,
    timestamp: Date.now(),
  });

  const child = spawn("bash", ["scripts/pull-model.sh", modelKey], {
    cwd: REPO,
    env: process.env,
    stdio: ["ignore", "pipe", "pipe"],
  });

  let stderrTail = "";
  child.stderr.on("data", (chunk: Buffer) => {
    stderrTail = (stderrTail + chunk.toString()).slice(-2000);
  });
  // We swallow stdout — ollama pull's progress format is opaque enough that
  // a generic "pulling" indicator is more honest than a parsed percentage.
  child.stdout.on("data", () => {});

  child.on("close", (code) => {
    if (code !== 0) {
      pushEvent(job, {
        phase: "failed",
        error: `pull-model.sh exited ${code}: ${stderrTail.trim().slice(-400)}`,
        timestamp: Date.now(),
      });
      return;
    }
    pushEvent(job, {
      phase: "done",
      message: `Pulled ${modelKey}`,
      timestamp: Date.now(),
    });
  });

  child.on("error", (err) => {
    pushEvent(job, {
      phase: "failed",
      error: `spawn failed: ${err.message}`,
      timestamp: Date.now(),
    });
  });
}

/**
 * Make a model the active default: write OLLAMA_MODEL into .env, patch
 * litellm/config.yaml so every claude-* mapping points at the new model,
 * and restart krull-litellm so it picks up the change.
 */
export async function selectActiveModel(modelKey: string): Promise<void> {
  // 1. Update .env
  const envPath = path.join(REPO, ".env");
  const parsed = await readEnvFile(envPath);
  setValue(parsed, "OLLAMA_MODEL", modelKey);
  await writeEnvFile(envPath, parsed);

  // 2. Patch litellm/config.yaml. Every line of the form
  //      model: openai/<something>
  //    becomes
  //      model: openai/<modelKey>
  const configPath = path.join(REPO, "litellm", "config.yaml");
  const original = await fs.readFile(configPath, "utf8");
  // Reject anything that isn't a plausible ollama model name to keep
  // the substitution safe.
  if (!/^[\w./:-]+$/.test(modelKey)) {
    throw new Error(`refusing to use unsafe model key: ${modelKey}`);
  }
  const patched = original.replace(
    /^(\s*model:\s*)openai\/[^\s#]+/gm,
    `$1openai/${modelKey}`,
  );
  if (patched !== original) {
    await fs.writeFile(configPath, patched, "utf8");
  }

  // 3. Restart litellm so the patched config takes effect.
  if (isRestartable("krull-litellm")) {
    await restartContainer("krull-litellm");
  }
}
