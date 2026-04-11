#!/usr/bin/env node
// Dark theme audit — spot-checks a broad set of kiwix/Krull pages
// for light-colored elements that escape the dark theme. Runs
// headless Chromium via Playwright, loads each URL (including the
// iframed content on /viewer pages), and reports anything whose
// visible paint is brighter than the palette allows.
//
// What it catches that a plain background-color sweep misses:
//   - background gradients (linear-/radial-/conic-gradient)
//   - background images pointing at light raster files
//   - light inline styles (background:#fff, style="bg..")
//   - visible light borders + outlines
//   - text that lands on a dark background with poor contrast
//   - <img> elements with inline light backgrounds
//
// Usage:
//   node scripts/dark-theme-audit.mjs                    # default URL list
//   node scripts/dark-theme-audit.mjs URL1 URL2 ...      # audit specific URLs
//
// The default URL list covers representative pages from every
// surface we care about: welcome, content (Wikipedia, Wiktionary,
// Wikivoyage, ArchWiki, Gutenberg, Stack Exchange, devdocs),
// viewer shell, search results, and the error takeover.
//
// Exit code: 0 if every page is clean, 1 if any page has issues.

// Resolve playwright from wherever it happens to live — either a
// local node_modules or a global install (mise / nvm / fnm). We
// don't want to force a package.json here; this script is a
// leaf tool.
import { createRequire } from "node:module";
import { execFileSync } from "node:child_process";

function loadPlaywright() {
  const require = createRequire(import.meta.url);
  // 1. Try the usual node_modules lookup from this file's location.
  try { return require("playwright"); } catch (_) {}
  // 2. Ask npm where its global node_modules directory is and try
  //    resolving from there. execFileSync (not execSync) is used
  //    so there's no shell interpolation — args are a fixed list.
  try {
    const globalRoot = execFileSync("npm", ["root", "-g"], { encoding: "utf8" }).trim();
    const globalRequire = createRequire(globalRoot + "/");
    return globalRequire("playwright");
  } catch (_) {}
  throw new Error(
    "playwright is not installed. Install it with one of:\n" +
    "  npm i -g playwright\n" +
    "  cd krull-home && npm i -D playwright  (then re-run from there)"
  );
}
const { chromium } = loadPlaywright();

const DEFAULT_URLS = [
  // Library chrome
  "http://localhost:8090/",
  // Viewer shell + iframed content (discovers iframe issues)
  "http://localhost:8090/viewer#archlinux_en_all_maxi_2025-09/Main_page",
  "http://localhost:8090/viewer#wikipedia_en_all_maxi_2026-02/Photosynthesis",
  // Direct content pages across ZIM styles
  "http://localhost:8090/content/wikipedia_en_all_maxi_2026-02/Albert_Einstein",
  "http://localhost:8090/content/wikipedia_en_all_maxi_2026-02/Quantum_mechanics",
  "http://localhost:8090/content/wikipedia_en_all_maxi_2026-02/Apollo_11",
  "http://localhost:8090/content/wiktionary_en_all_nopic_2026-02/Iran",
  "http://localhost:8090/content/wiktionary_en_all_nopic_2026-02/water",
  // Search results + friendly error
  "http://localhost:8090/search?pattern=ireland&books.name=wikipedia_en_all_maxi_2026-02",
  "http://localhost:8090/search?pattern=test",
];

// Palette brightness threshold. Anything averaging above this
// on a dark-theme surface is suspect. 180/255 ≈ 70% — catches
// obvious whites and pastels, skips intentional parchment-gold
// (#d4a574 → avg 160) and the kiwix mw-no-invert swatches.
const BRIGHT_THRESHOLD = 170;

// Classes/attributes we intentionally skip. Data-viz swatches
// carry meaning through color and shouldn't be darkened.
const IGNORE_CLASS_SUBSTRINGS = [
  "mw-no-invert", // explicit opt-out from Wikipedia dark mode
  "legend-color", // chart legends
  "pie25", "pie50", "pie75", // pie chart slices
  "l-color", // flag color swatches
  "skin-invert-image", // wrapper we deliberately leave transparent
];

// Sweep a document for all classes of dark-theme violations.
// Runs inside the page context via page.evaluate.
async function auditDocument(frameOrPage) {
  const pageScript = ({ threshold, ignoreClasses }) => {
    const parseColor = (str) => {
      if (!str || str === "transparent") return null;
      const m = str.match(/rgba?\s*\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)(?:\s*,\s*([\d.]+))?\s*\)/);
      if (!m) return null;
      return [parseInt(m[1]), parseInt(m[2]), parseInt(m[3]), m[4] === undefined ? 1 : parseFloat(m[4])];
    };
    const brightness = (rgb) => (rgb[0] + rgb[1] + rgb[2]) / 3;
    const shouldIgnore = (el) => {
      const cls = typeof el.className === "string" ? el.className : "";
      for (const needle of ignoreClasses) if (cls.indexOf(needle) !== -1) return true;
      return false;
    };

    const findings = [];
    const seen = new Set();
    const rec = (type, el, detail) => {
      const cls = (typeof el.className === "string" ? el.className : "").substring(0, 60);
      const key = type + "|" + el.tagName + "." + cls + "|" + detail;
      if (seen.has(key)) return;
      seen.add(key);
      findings.push({ type, tag: el.tagName.toLowerCase(), class: cls, detail });
    };

    for (const el of document.querySelectorAll("*")) {
      if (shouldIgnore(el)) continue;
      // Skip invisible elements — display:none / visibility:hidden
      // or zero-sized. They can't visually pollute.
      const cs = window.getComputedStyle(el);
      if (cs.display === "none" || cs.visibility === "hidden") continue;
      const rect = el.getBoundingClientRect();
      if (rect.width === 0 || rect.height === 0) continue;

      // 1. Light background-color
      const bg = parseColor(cs.backgroundColor);
      if (bg && bg[3] > 0.1 && brightness(bg) > threshold) {
        rec("bg-color", el, cs.backgroundColor);
      }

      // 2. Light background-image gradients
      // We only flag gradients containing light colors — bg-image
      // URLs to PNG/SVG icons are left alone since icons are
      // designed to be visible on any background and there are
      // thousands of them in ZIM content (external-link arrows,
      // book covers, padlock icons, etc.).
      const bgImg = cs.backgroundImage;
      if (bgImg && bgImg !== "none") {
        const colors = bgImg.match(/rgba?\s*\([^)]+\)/g) || [];
        for (const c of colors) {
          const rgb = parseColor(c);
          if (rgb && rgb[3] > 0.1 && brightness(rgb) > threshold) {
            rec("bg-gradient", el, bgImg.substring(0, 80));
            break;
          }
        }
      }

      // 3. Light visible borders — but skip dotted/dashed since
      // those are typographic cues (Wikipedia citation tooltips,
      // abbreviation hints) and not dark-theme violations.
      for (const side of ["Top", "Right", "Bottom", "Left"]) {
        const w = parseFloat(cs["border" + side + "Width"]);
        const style = cs["border" + side + "Style"];
        if (w > 0 && style !== "none" && style !== "dotted" && style !== "dashed") {
          const bc = parseColor(cs["border" + side + "Color"]);
          if (bc && bc[3] > 0.1 && brightness(bc) > threshold) {
            rec("border", el, side + " " + cs["border" + side + "Color"]);
            break;
          }
        }
      }

      // 4. Light outlines
      const ow = parseFloat(cs.outlineWidth);
      if (ow > 0 && cs.outlineStyle !== "none") {
        const oc = parseColor(cs.outlineColor);
        if (oc && brightness(oc) > threshold) {
          rec("outline", el, cs.outlineColor);
        }
      }

      // 5. <img> with inline light background (transparent PNGs
      // hosted on a light element)
      if (el.tagName === "IMG") {
        const inline = el.getAttribute("style") || "";
        if (/background\s*(-color)?\s*:\s*#(f|e|d)/i.test(inline)) {
          rec("img-inline-bg", el, inline.substring(0, 60));
        }
      }
    }

    return findings;
  };

  return frameOrPage.evaluate(pageScript, {
    threshold: BRIGHT_THRESHOLD,
    ignoreClasses: IGNORE_CLASS_SUBSTRINGS,
  });
}

async function auditUrl(browser, url) {
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
  const results = { url, shell: [], iframe: [], error: null };
  try {
    await page.goto(url, { waitUntil: "networkidle", timeout: 20000 });
    // Extra settle time for the /viewer iframe to navigate from
    // /skin/blank.html to the actual content.
    await page.waitForTimeout(800);

    results.shell = await auditDocument(page);

    // If the page has #content_iframe (kiwix /viewer), audit it too.
    const frames = page.frames().filter((f) => f !== page.mainFrame());
    for (const f of frames) {
      if (!f.url() || f.url().includes("blank.html")) continue;
      try {
        const iframeFindings = await auditDocument(f);
        results.iframe.push(...iframeFindings);
      } catch (_e) {
        // Cross-origin or detached frame — skip.
      }
    }
  } catch (e) {
    results.error = e.message;
  } finally {
    await page.close();
  }
  return results;
}

function formatFindings(findings) {
  if (findings.length === 0) return "  clean";
  return findings
    .map((f) => `  [${f.type}] ${f.tag}.${f.class || "-"} → ${f.detail}`)
    .join("\n");
}

async function main() {
  const urls = process.argv.slice(2).length > 0 ? process.argv.slice(2) : DEFAULT_URLS;
  const browser = await chromium.launch();
  let anyIssues = false;

  console.log(`\nDark theme audit — ${urls.length} pages\n`);
  for (const url of urls) {
    process.stdout.write(`→ ${url}\n`);
    const result = await auditUrl(browser, url);
    if (result.error) {
      console.log(`  ERROR: ${result.error}`);
      anyIssues = true;
      continue;
    }
    const total = result.shell.length + result.iframe.length;
    if (total === 0) {
      console.log("  clean");
    } else {
      anyIssues = true;
      if (result.shell.length) {
        console.log("  shell:");
        console.log(formatFindings(result.shell).split("\n").map((l) => "  " + l).join("\n"));
      }
      if (result.iframe.length) {
        console.log("  iframe:");
        console.log(formatFindings(result.iframe).split("\n").map((l) => "  " + l).join("\n"));
      }
    }
    console.log("");
  }

  await browser.close();
  process.exit(anyIssues ? 1 : 0);
}

main().catch((e) => {
  console.error(e);
  process.exit(2);
});
