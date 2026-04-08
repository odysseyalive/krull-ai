/**
 * Parse the bash CATALOG arrays out of scripts/download-*.sh and merge them
 * into a normalized JSON catalog. The bash script remains the source of
 * truth — this parser runs at server startup and on every /api/library
 * request, so any change to the scripts is picked up immediately.
 */
import fs from "node:fs/promises";
import path from "node:path";

export type PackageKind = "knowledge" | "wikipedia" | "maps";

export interface CatalogPackage {
  kind: PackageKind;
  key: string;
  name: string;
  description: string;
  size: string;
  /** Filename on disk relative to its target directory */
  file: string;
  /** Target directory under repo root, e.g. "zim" or "data/tiles" */
  targetDir: string;
  /** Optional category tag for grouping in the UI */
  category?: string;
  installed?: boolean;
  installedSizeBytes?: number;
  /** If a file exists on disk but failed format/integrity inspection */
  corrupt?: "html" | "magic" | "truncated" | "unreadable" | string;
  /** Bytes currently on disk when the file is corrupt (esp. truncated)
   *  so the UI can compute a "Resume (X%)" label against the expected
   *  catalog size without having to stat the file itself. */
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

const SCRIPTS = {
  knowledge: "scripts/download-knowledge.sh",
  wikipedia: "scripts/download-wikipedia.sh",
  maps: "scripts/download-maps.sh",
} as const;

/**
 * Extract a bash array body by name. Returns the contents between
 * `NAME=(` and the matching `)`. Lines starting with # inside are
 * preserved so the caller can use them as group separators.
 */
function extractArray(text: string, name: string): string[] {
  const re = new RegExp(`^${name}=\\(\\s*$`, "m");
  const m = re.exec(text);
  if (!m) return [];
  const start = m.index + m[0].length;
  const rest = text.slice(start);
  const endRe = /^\s*\)\s*$/m;
  const end = endRe.exec(rest);
  if (!end) return [];
  return rest
    .slice(0, end.index)
    .split("\n")
    .map((l) => l.trim())
    .filter((l) => l.length > 0);
}

function unquote(s: string): string {
  if (s.length >= 2 && s.startsWith('"') && s.endsWith('"')) {
    return s.slice(1, -1);
  }
  return s;
}

function parseKnowledge(text: string): {
  packages: CatalogPackage[];
  bundles: CatalogBundle[];
} {
  const packages: CatalogPackage[] = [];
  const lines = extractArray(text, "CATALOG");
  let category = "";
  for (const line of lines) {
    if (line.startsWith("#")) {
      category = line.replace(/^#+\s*/, "").trim();
      continue;
    }
    const raw = unquote(line);
    const parts = raw.split("|");
    if (parts.length < 4) continue;
    const [key, file, description, size] = parts;
    packages.push({
      kind: "knowledge",
      key,
      name: prettyName(key),
      description,
      size,
      file: path.basename(file),
      targetDir: "zim",
      category: category || undefined,
    });
  }

  const bundles: CatalogBundle[] = [];
  const bundleSizes: Record<string, string> = {
    "dev-essentials": "~50 MB",
    "web-dev": "~58 MB",
    "krull-stack": "~20 MB",
    "data-science": "~75 MB",
    sysadmin: "~5.5 GB",
    community: "~5 GB",
    "survival-essentials": "~1.9 GB",
    "cooking-essentials": "~360 MB",
    "gutenberg-essentials": "~65 GB",
    "gutenberg-stem": "~22 GB",
    "gutenberg-all-english": "~212 GB",
    anthropology: "~24 GB",
  };
  const bundleDescs: Record<string, string> = {
    "dev-essentials": "Core developer docs",
    "web-dev": "Web development stack",
    "krull-stack": "Everything this repo actually uses",
    "data-science": "Data science & ML",
    sysadmin: "System administration",
    community: "Developer Q&A",
    "survival-essentials": "Survival, navigation, medicine, self-sufficiency",
    "cooking-essentials": "Cooking knowledge & recipes",
    "gutenberg-essentials": "Classic literature & reference",
    "gutenberg-stem": "Science, technology, medicine",
    "gutenberg-all-english":
      "All 18 Library of Congress categories as resumable pieces (equivalent to the 206 GB monolith, split by subject)",
    anthropology:
      "Cultural, archaeological, linguistic & psychological anthropology",
  };
  const caseRe = /(\S[\S-]*)\)\s*\n\s*echo\s+"([^"]+)"\s*;;/g;
  let caseMatch: RegExpExecArray | null;
  while ((caseMatch = caseRe.exec(text)) !== null) {
    const [, name, members] = caseMatch;
    if (name === "*") continue;
    if (!bundleSizes[name]) continue;
    bundles.push({
      kind: "knowledge",
      key: name,
      name: prettyName(name),
      description: bundleDescs[name] ?? "",
      size: bundleSizes[name] ?? "",
      members: members.trim().split(/\s+/),
    });
  }

  return { packages, bundles };
}

function parseWikipedia(text: string): CatalogPackage[] {
  const out: CatalogPackage[] = [];
  const re = /^\s*(mini|nopic|medicine|full)\)\s*$([\s\S]*?);;/gm;
  let m: RegExpExecArray | null;
  while ((m = re.exec(text)) !== null) {
    const [, key, body] = m;
    const fileMatch = /FILE="([^"]+)"/.exec(body);
    const descMatch = /DESC="([^"]+)"/.exec(body);
    if (!fileMatch || !descMatch) continue;
    const desc = descMatch[1];
    // Pull any "~5 MB"-style size token from anywhere in the description.
    const sizeMatch = /(~?\s*[\d.]+\s*[KMGT]?B)\b/.exec(desc);
    out.push({
      kind: "wikipedia",
      key,
      name: prettyWikipediaName(key),
      description: desc.replace(/\s*\([^)]*\)\s*$/, "").trim(),
      size: sizeMatch?.[1].trim() ?? "",
      file: fileMatch[1],
      targetDir: "zim",
    });
  }
  return out;
}

/**
 * Parse a map-region bbox field from the catalog entry. Returns null
 * for entries like "planet" that have an empty bbox — those skip the
 * auto-detect logic in the bash script.
 */
function parseBbox(
  bboxStr: string | undefined,
): [number, number, number, number] | null {
  if (!bboxStr) return null;
  const parts = bboxStr.split(",").map((s) => parseFloat(s));
  if (parts.length !== 4 || parts.some((n) => !Number.isFinite(n))) return null;
  return parts as [number, number, number, number];
}

/** Standard two-axis-aligned-bounding-box overlap test. */
function bboxesOverlap(
  a: [number, number, number, number],
  b: [number, number, number, number],
): boolean {
  const [aW, aS, aE, aN] = a;
  const [bW, bS, bE, bN] = b;
  return aW < bE && aE > bW && aS < bN && aN > bS;
}

/**
 * Parse chart size fields like "596" (MB), "~5 GB", "~100 MB", "~800 MB".
 * Returns the size in megabytes. Returns 0 for unparseable input.
 */
function parseSizeToMb(s: string | undefined): number {
  if (!s) return 0;
  const m = /([\d.]+)\s*(KB|MB|GB|TB|B)?/i.exec(s);
  if (!m) return 0;
  const value = parseFloat(m[1]);
  if (!Number.isFinite(value)) return 0;
  const unit = (m[2] ?? "MB").toUpperCase();
  switch (unit) {
    case "KB":
      return value / 1024;
    case "MB":
      return value;
    case "GB":
      return value * 1024;
    case "TB":
      return value * 1024 * 1024;
    default:
      return value;
  }
}

/** Format a megabyte number as a short human-readable size string. */
function formatMb(mb: number): string {
  if (mb >= 1024) {
    const gb = mb / 1024;
    return gb >= 10 ? `~${Math.round(gb)} GB` : `~${gb.toFixed(1)} GB`;
  }
  if (mb >= 10) return `~${Math.round(mb)} MB`;
  return `~${mb.toFixed(1)} MB`;
}

interface ChartSection {
  id: string;
  sizeMb: number;
  bbox: [number, number, number, number];
}

/**
 * Parse a bbox-keyed bash array like NCDS_SECTIONS or FAA_SECTIONS.
 * Line format:  "id|description|size_mb|west,south,east,north"
 */
function parseChartSections(text: string, arrayName: string): ChartSection[] {
  const out: ChartSection[] = [];
  const lines = extractArray(text, arrayName);
  for (const line of lines) {
    if (line.startsWith("#")) continue;
    const raw = unquote(line);
    const parts = raw.split("|");
    if (parts.length < 4) continue;
    const [id, , sizeStr, bboxStr] = parts;
    const bbox = parseBbox(bboxStr);
    if (!bbox) continue;
    const sizeMb = parseSizeToMb(sizeStr);
    out.push({ id, sizeMb, bbox });
  }
  return out;
}

/**
 * Global terrain PMTiles downloaded by the default path for every
 * region. The bash script hardcodes this as "z0-9, ~700 MB" — see
 * download_terrain() in scripts/download-maps.sh.
 */
const GLOBAL_TERRAIN_MB = 700;

function parseMaps(text: string): CatalogPackage[] {
  // Parse auxiliary arrays first so we can compute overlap totals per region.
  const ncdsSections = parseChartSections(text, "NCDS_SECTIONS");
  const faaSections = parseChartSections(text, "FAA_SECTIONS");

  const out: CatalogPackage[] = [];
  const lines = extractArray(text, "CATALOG");
  for (const line of lines) {
    if (line.startsWith("#")) continue;
    const raw = unquote(line);
    const parts = raw.split("|");
    if (parts.length < 3) continue;
    const [key, description, sizeStr, bboxStr] = parts;

    const baseMb = parseSizeToMb(sizeStr);
    const bbox = parseBbox(bboxStr);

    // Replicate the bash script's default download path:
    //   base OSM tiles + global terrain + auto-detected nautical + aero.
    // Regions with an empty bbox (like "planet") skip the nautical and
    // aero auto-detection — matches the `if [ -n "$REGION_BBOX" ]`
    // guard in the script.
    let nauticalMb = 0;
    let aeroMb = 0;
    if (bbox) {
      nauticalMb = ncdsSections
        .filter((s) => bboxesOverlap(bbox, s.bbox))
        .reduce((sum, s) => sum + s.sizeMb, 0);
      aeroMb = faaSections
        .filter((s) => bboxesOverlap(bbox, s.bbox))
        .reduce((sum, s) => sum + s.sizeMb, 0);
    }

    const totalMb = baseMb + GLOBAL_TERRAIN_MB + nauticalMb + aeroMb;

    // Build a human-readable description that shows the breakdown so
    // the user knows what they're actually getting.
    const breakdown: string[] = [];
    if (baseMb > 0) breakdown.push(`${formatMb(baseMb)} base`);
    breakdown.push(`${formatMb(GLOBAL_TERRAIN_MB)} terrain`);
    if (nauticalMb > 0) breakdown.push(`${formatMb(nauticalMb)} nautical`);
    if (aeroMb > 0) breakdown.push(`${formatMb(aeroMb)} aero`);

    out.push({
      kind: "maps",
      key,
      name: description,
      description: `Offline maps for ${description}. Includes ${breakdown.join(" + ")}.`,
      size: formatMb(totalMb),
      file: `${key}.pmtiles`,
      targetDir: "data/tiles",
      category: "Base regions",
    });
  }
  return out;
}

function prettyName(key: string): string {
  // TED keeps its all-caps brand when we strip the prefix. Handled
  // before the generic title-caser runs so "TED" doesn't become "Ted".
  if (key.startsWith("ted-")) {
    const rest = key
      .slice(4)
      .replace(/-/g, " ")
      .replace(/\b\w/g, (c) => c.toUpperCase());
    return `TED — ${rest}`;
  }
  return key
    .replace(/^devdocs-/, "")
    .replace(/^stackexchange-/, "Stack Exchange — ")
    .replace(/^gutenberg-?/, "Gutenberg — ")
    .replace(/-/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

function prettyWikipediaName(key: string): string {
  switch (key) {
    case "mini":
      return "Wikipedia Mini";
    case "nopic":
      return "Wikipedia (no images)";
    case "medicine":
      return "Wikipedia Medicine";
    case "full":
      return "Wikipedia Full (with images)";
    default:
      return `Wikipedia ${key}`;
  }
}

/**
 * Inspect a downloaded file and return whether it's the kind of file
 * the catalog says it should be.
 *
 * For ZIM files (knowledge / wikipedia) we check both magic bytes AND
 * truncation. The ZIM header at offset 72 holds a uint64 pointing at
 * the file's trailing 16-byte MD5 checksum; if the file is shorter
 * than checksumPos + 16, the download was interrupted.
 *
 * Returns:
 *   { ok: true, sizeBytes }                — looks valid
 *   { ok: false, reason: "missing" }       — file isn't there
 *   { ok: false, reason: "html" }          — got an HTML error page
 *   { ok: false, reason: "magic" }         — wrong file format
 *   { ok: false, reason: "truncated" }     — partial download
 */
async function inspectPackageFile(
  filePath: string,
  kind: PackageKind,
): Promise<
  | { ok: true; sizeBytes: number }
  | { ok: false; reason: string; sizeBytes?: number }
> {
  let stat;
  try {
    stat = await fs.stat(filePath);
  } catch {
    return { ok: false, reason: "missing" };
  }

  let fh;
  try {
    fh = await fs.open(filePath, "r");
  } catch {
    return { ok: false, reason: "unreadable", sizeBytes: stat.size };
  }
  try {
    const head = Buffer.alloc(80);
    const { bytesRead } = await fh.read(head, 0, 80, 0);
    if (bytesRead < 4) {
      return { ok: false, reason: "truncated", sizeBytes: stat.size };
    }

    // HTML 404 pages from a stale mirror look like text starting with
    // "<!DOCTYPE" or "<html" — reject those before any format checks.
    const headStr = head.slice(0, Math.min(bytesRead, 16)).toString("utf8");
    if (/^\s*<(?:!doctype|html)/i.test(headStr)) {
      return { ok: false, reason: "html", sizeBytes: stat.size };
    }

    if (kind === "knowledge" || kind === "wikipedia") {
      // ZIM magic: 5A 49 4D 04
      if (
        head[0] !== 0x5a ||
        head[1] !== 0x49 ||
        head[2] !== 0x4d ||
        head[3] !== 0x04
      ) {
        return { ok: false, reason: "magic", sizeBytes: stat.size };
      }
      if (bytesRead >= 80) {
        // ZIM header offset 72: uint64 little-endian, position of the
        // trailing MD5 checksum block (16 bytes).
        const checksumPos = Number(head.readBigUInt64LE(72));
        if (stat.size < checksumPos + 16) {
          return { ok: false, reason: "truncated", sizeBytes: stat.size };
        }
      }
    }

    if (kind === "maps") {
      // PMTiles magic: ASCII "PMTiles"
      if (
        bytesRead < 7 ||
        head.slice(0, 7).toString("ascii") !== "PMTiles"
      ) {
        return { ok: false, reason: "magic", sizeBytes: stat.size };
      }
    }

    return { ok: true, sizeBytes: stat.size };
  } finally {
    await fh.close();
  }
}

export async function loadCatalog(repo: string): Promise<Catalog> {
  const [knowledgeText, wikipediaText, mapsText] = await Promise.all([
    fs.readFile(path.join(repo, SCRIPTS.knowledge), "utf8"),
    fs.readFile(path.join(repo, SCRIPTS.wikipedia), "utf8"),
    fs.readFile(path.join(repo, SCRIPTS.maps), "utf8"),
  ]);

  const knowledge = parseKnowledge(knowledgeText);
  const wikipedia = parseWikipedia(wikipediaText);
  const maps = parseMaps(mapsText);

  const packages = [...knowledge.packages, ...wikipedia, ...maps];

  await Promise.all(
    packages.map(async (pkg) => {
      const full = path.join(repo, pkg.targetDir, pkg.file);
      const result = await inspectPackageFile(full, pkg.kind);
      if (result.ok) {
        pkg.installed = true;
        pkg.installedSizeBytes = result.sizeBytes;
      } else {
        pkg.installed = false;
        // A file that exists on disk but failed inspection is *corrupt*
        // — it's important to surface this distinct state so the UI
        // can offer a re-install instead of pretending nothing's there.
        if (result.reason !== "missing") {
          pkg.corrupt = result.reason;
          if (typeof result.sizeBytes === "number") {
            pkg.partialBytes = result.sizeBytes;
          }
        }
      }
    }),
  );

  return { packages, bundles: knowledge.bundles };
}
