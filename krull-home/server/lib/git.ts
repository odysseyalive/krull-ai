/**
 * Read the current git HEAD without needing the git binary inside
 * krull-home. We just walk the bind-mounted .git directory.
 */
import fs from "node:fs/promises";
import path from "node:path";

export interface GitInfo {
  commit: string;
  shortCommit: string;
  branch: string | null;
}

export async function readGitInfo(repoRoot: string): Promise<GitInfo | null> {
  try {
    const headPath = path.join(repoRoot, ".git", "HEAD");
    const head = (await fs.readFile(headPath, "utf8")).trim();

    let commit: string;
    let branch: string | null = null;

    if (head.startsWith("ref: ")) {
      const ref = head.slice(5).trim();
      branch = ref.replace(/^refs\/heads\//, "");
      try {
        commit = (
          await fs.readFile(path.join(repoRoot, ".git", ref), "utf8")
        ).trim();
      } catch {
        // Packed refs fallback — scan packed-refs for the ref
        const packed = await fs
          .readFile(path.join(repoRoot, ".git", "packed-refs"), "utf8")
          .catch(() => "");
        const line = packed
          .split("\n")
          .find((l) => l.endsWith(` ${ref}`));
        if (!line) return null;
        commit = line.split(" ")[0];
      }
    } else {
      // Detached HEAD — head IS the commit hash
      commit = head;
    }

    return {
      commit,
      shortCommit: commit.slice(0, 7),
      branch,
    };
  } catch {
    return null;
  }
}
