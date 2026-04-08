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
    // Oxford University graduate-subject bundles (Humanities division)
    "oxford-anthropology": "~24 GB",
    "oxford-archaeology": "~8.4 GB",
    "oxford-history-of-art": "~37 GB",
    "oxford-asian-middle-eastern-studies": "~39 GB",
    "oxford-classics": "~38 GB",
    "oxford-english-language-literature": "~37 GB",
    "oxford-history": "~54 GB",
    "oxford-law": "~2.5 GB",
    "oxford-linguistics": "~8.9 GB",
    "oxford-medieval-modern-languages": "~10.2 GB",
    "oxford-music": "~15 GB",
    "oxford-philosophy": "~8.5 GB",
    "oxford-theology-religion": "~8.1 GB",
    "oxford-fine-art": "~37 GB",
    // Oxford University graduate-subject bundles (Mathematical, Physical and Life Sciences)
    "oxford-biology": "~14.8 GB",
    "oxford-chemistry": "~14.5 GB",
    "oxford-computer-science": "~76 GB",
    "oxford-earth-sciences": "~12.2 GB",
    "oxford-engineering": "~11 GB",
    "oxford-materials": "~12.1 GB",
    "oxford-mathematics": "~20 GB",
    "oxford-physics": "~14.5 GB",
    "oxford-statistics": "~1.7 GB",
    // Oxford University graduate-subject bundles (Medical Sciences)
    "oxford-clinical-medicine": "~5.2 GB",
    "oxford-clinical-neurosciences": "~8 GB",
    "oxford-medicine": "~5.3 GB",
    "oxford-neuroscience": "~5 GB",
    // Oxford University graduate-subject bundles (Social Sciences)
    "oxford-economics": "~14 GB",
    "oxford-education": "~8.2 GB",
    "oxford-geography-environment": "~18.6 GB",
    "oxford-global-area-studies": "~39 GB",
    "oxford-government": "~7.9 GB",
    "oxford-international-development": "~11.7 GB",
    "oxford-politics-international-relations": "~9.7 GB",
    "oxford-social-policy-intervention": "~11.7 GB",
    "oxford-sociology": "~9.8 GB",
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
    // Oxford University graduate-subject bundles (Humanities division)
    "oxford-anthropology":
      "Cultural, archaeological, linguistic & psychological anthropology (four-field model)",
    "oxford-archaeology":
      "Field methodology talks plus LoC G primary texts for excavation history and theory",
    "oxford-history-of-art":
      "Gutenberg LoC N — the canonical art history and criticism collection",
    "oxford-asian-middle-eastern-studies":
      "Regional TED collections, Gutenberg LoC D (world history including Asia/Middle East), and LoC PL (Eastern languages/literatures)",
    "oxford-classics":
      "Gutenberg LoC PA (Greek and Latin language and literature), Latin Stack Exchange, and LoC D for ancient world history",
    "oxford-english-language-literature":
      "Gutenberg LoC PR (British) + PS (American) + PN (general/drama/criticism) plus Literature Stack Exchange for close-reading Q&A",
    "oxford-history":
      "Gutenberg LoC D (world), E (US), C (auxiliary historical sciences), Wikipedia history subset, plus History and History-of-Science Stack Exchanges",
    "oxford-law":
      "Gutenberg LoC K (jurisprudence and legal history) plus Law Stack Exchange for doctrinal Q&A",
    "oxford-linguistics":
      "Linguistics Stack Exchange, Wiktionary (full lexicographic reference), Latin SE for historical philology, and Gutenberg LoC PA",
    "oxford-medieval-modern-languages":
      "Gutenberg LoC PQ (French/Italian/Spanish/Portuguese), PT (Germanic/Scandinavian), and PC (Romance philology)",
    "oxford-music":
      "Gutenberg LoC M (musicology), Mutopia Project (2100+ free classical scores), Open Music Theory textbook, Music SE, and TED music talks",
    "oxford-philosophy":
      "Gutenberg LoC B (philosophy/psychology/religion canon), Internet Encyclopedia of Philosophy (peer-reviewed), Philosophy SE, and TED philosophy/ethics",
    "oxford-theology-religion":
      "Gutenberg LoC B (religion and philosophy) plus comparative-religion Stack Exchanges (Buddhism, Hinduism, Judaism) and TED religion talks",
    "oxford-fine-art":
      "Gutenberg LoC N — canonical art, criticism and practice texts",
    // Oxford University graduate-subject bundles (Mathematical, Physical and Life Sciences)
    "oxford-biology":
      "LibreTexts Biology, Biology Stack Exchange, Wikipedia molecular/cell biology subset, and Gutenberg science canon",
    "oxford-chemistry":
      "LibreTexts Chemistry, Chemistry Stack Exchange, Wikipedia chemistry subset, and Gutenberg science canon",
    "oxford-computer-science":
      "Jeff Erickson's Algorithms textbook, CS and Theoretical CS Stack Exchanges, and the full Stack Overflow archive",
    "oxford-earth-sciences":
      "Earth Science Stack Exchange plus Gutenberg LoC G (physical geography) and science canon",
    "oxford-engineering":
      "LibreTexts Engineering, Engineering and Electronics Stack Exchanges, and Gutenberg technology canon (LoC T)",
    "oxford-materials":
      "Matter Modeling Stack Exchange (computational materials science) plus technology and science canon",
    "oxford-mathematics":
      "Math Stack Exchange (6.9 GB canonical community), LibreTexts Mathematics, PlanetMath encyclopedia, Wikipedia math subset, and Gutenberg science",
    "oxford-physics":
      "LibreTexts Physics, Physics Stack Exchange, Wikipedia physics subset, and Gutenberg science canon",
    "oxford-statistics":
      "Cross Validated (stats.stackexchange), LibreTexts Statistics, and Learning Statistics with R",
    // Oxford University graduate-subject bundles (Medical Sciences; Clinical Psychology has no Kiwix coverage)
    "oxford-clinical-medicine":
      "LibreTexts Medicine, Medical Sciences SE, Surviving Residency guides, WikEM, plus field and military medicine",
    "oxford-clinical-neurosciences":
      "TED Brain collection, Psychology/Neuroscience SE, LibreTexts Medicine neurology, and Gutenberg medicine",
    "oxford-medicine":
      "LibreTexts Medicine, Libre Pathology, Medical Sciences SE, Surviving Residency, WikEM, and historical/field medicine",
    "oxford-neuroscience":
      "TED Brain, Psychology/Neuroscience SE, and LibreTexts Medicine neuroanatomy/neurology chapters",
    // Oxford University graduate-subject bundles (Social Sciences)
    "oxford-economics":
      "Gutenberg LoC H (social sciences), Economics SE, TED Economics and Behavioral Economics",
    "oxford-education":
      "Gutenberg LoC L (education), TED Education, Academia SE, and LibreTexts K-12",
    "oxford-geography-environment":
      "Gutenberg LoC G, Wikipedia geography, Encyclopedia of the Environment, TED Environment, and Wikivoyage",
    "oxford-global-area-studies":
      "Regional TED collections (Asia, Middle East), Gutenberg LoC D (world history), and LoC PL (Eastern languages)",
    "oxford-government":
      "Gutenberg LoC J (political science), TED Government, and Politics SE",
    "oxford-international-development":
      "TED International Development, CD3WD sustainable-development archive, Gutenberg LoC H, and Appropedia",
    "oxford-politics-international-relations":
      "Gutenberg LoC J, TED Politics and International Relations, and Politics SE",
    "oxford-social-policy-intervention":
      "TED Public Health, Gutenberg LoC H, and Appropedia for evidence-based intervention work",
    "oxford-sociology":
      "Gutenberg LoC H (sociology canon), TED Sociology, and Wikipedia sociology subset",
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
