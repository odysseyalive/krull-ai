/**
 * Tokenized .env reader/writer that preserves order, comments, and blank lines.
 *
 * Each line of the file is parsed into a token. Updating a value mutates the
 * matching token in place; new keys are appended at the end. Comments and
 * blank lines are never touched.
 */
import fs from "node:fs/promises";
import path from "node:path";

export type EnvToken =
  | { kind: "blank" }
  | { kind: "comment"; text: string }
  | { kind: "kv"; key: string; raw: string; value: string };

export interface ParsedEnv {
  tokens: EnvToken[];
}

const KV_RE = /^\s*([A-Z_][A-Z0-9_]*)\s*=\s*(.*?)\s*$/i;

function unquote(raw: string): string {
  if (raw.length >= 2) {
    const first = raw[0];
    const last = raw[raw.length - 1];
    if ((first === '"' && last === '"') || (first === "'" && last === "'")) {
      return raw.slice(1, -1);
    }
  }
  return raw;
}

function quoteIfNeeded(value: string): string {
  // Quote if value contains whitespace, #, or is empty.
  if (value === "") return '""';
  if (/[\s#"']/.test(value)) {
    return `"${value.replace(/"/g, '\\"')}"`;
  }
  return value;
}

export function parseEnv(text: string): ParsedEnv {
  const tokens: EnvToken[] = [];
  for (const line of text.split(/\r?\n/)) {
    if (line.trim() === "") {
      tokens.push({ kind: "blank" });
      continue;
    }
    if (line.trim().startsWith("#")) {
      tokens.push({ kind: "comment", text: line });
      continue;
    }
    const m = line.match(KV_RE);
    if (m) {
      tokens.push({
        kind: "kv",
        key: m[1],
        raw: m[2],
        value: unquote(m[2]),
      });
      continue;
    }
    // Treat unparseable lines as comments to avoid losing them.
    tokens.push({ kind: "comment", text: line });
  }
  // Drop trailing blank if file ended with a newline (we'll re-add on serialize).
  if (tokens.length && tokens[tokens.length - 1].kind === "blank") {
    tokens.pop();
  }
  return { tokens };
}

export function serializeEnv(parsed: ParsedEnv): string {
  const lines = parsed.tokens.map((t) => {
    if (t.kind === "blank") return "";
    if (t.kind === "comment") return t.text;
    return `${t.key}=${t.raw}`;
  });
  return lines.join("\n") + "\n";
}

export function getValue(parsed: ParsedEnv, key: string): string | undefined {
  for (const t of parsed.tokens) {
    if (t.kind === "kv" && t.key === key) return t.value;
  }
  return undefined;
}

export function setValue(parsed: ParsedEnv, key: string, value: string): void {
  for (const t of parsed.tokens) {
    if (t.kind === "kv" && t.key === key) {
      t.value = value;
      t.raw = quoteIfNeeded(value);
      return;
    }
  }
  parsed.tokens.push({ kind: "kv", key, value, raw: quoteIfNeeded(value) });
}

export function listEntries(parsed: ParsedEnv): Array<{ key: string; value: string }> {
  const out: Array<{ key: string; value: string }> = [];
  for (const t of parsed.tokens) {
    if (t.kind === "kv") out.push({ key: t.key, value: t.value });
  }
  return out;
}

export async function readEnvFile(filePath: string): Promise<ParsedEnv> {
  try {
    const text = await fs.readFile(filePath, "utf8");
    return parseEnv(text);
  } catch (err) {
    const e = err as NodeJS.ErrnoException;
    if (e.code === "ENOENT") return { tokens: [] };
    throw err;
  }
}

export async function writeEnvFile(filePath: string, parsed: ParsedEnv): Promise<void> {
  const text = serializeEnv(parsed);
  // Atomic write: tmp file in same dir + rename.
  const dir = path.dirname(filePath);
  const tmp = path.join(dir, `.env.tmp.${process.pid}.${Date.now()}`);
  await fs.writeFile(tmp, text, { encoding: "utf8", mode: 0o644 });
  await fs.rename(tmp, filePath);
}
