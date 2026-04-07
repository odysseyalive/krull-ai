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

function parseMaps(text: string): CatalogPackage[] {
  const out: CatalogPackage[] = [];
  const lines = extractArray(text, "CATALOG");
  for (const line of lines) {
    if (line.startsWith("#")) continue;
    const raw = unquote(line);
    const parts = raw.split("|");
    if (parts.length < 3) continue;
    const [key, description, size] = parts;
    out.push({
      kind: "maps",
      key,
      name: description,
      description: `Base OSM map tiles for ${description}.`,
      size,
      file: `${key}.pmtiles`,
      targetDir: "data/tiles",
      category: "Base regions",
    });
  }
  return out;
}

function prettyName(key: string): string {
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
      try {
        const st = await fs.stat(full);
        pkg.installed = true;
        pkg.installedSizeBytes = st.size;
      } catch {
        pkg.installed = false;
      }
    }),
  );

  return { packages, bundles: knowledge.bundles };
}
