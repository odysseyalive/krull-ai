import { Map, Popup } from 'maplibre-gl';
import { VectorTile } from '@mapbox/vector-tile';
import Pbf from 'pbf';

const PHOTON_URL = window.location.protocol + '//' + window.location.hostname + ':2322';
const TILE_API = window.location.origin;

interface SearchResult {
  name: string;
  detail: string;
  type: string;
  coords: [number, number];
  source: 'map' | 'geocoder' | 'index';
}

interface PhotonFeature {
  type: string;
  geometry: { type: string; coordinates: [number, number] };
  properties: Record<string, string>;
}

interface PlaceEntry {
  name: string;
  kind: string;
  coords: [number, number];
}

let placeIndex: PlaceEntry[] = [];
let indexReady = false;

// Convert tile pixel coords to lng/lat
function tileCoordsToLngLat(
  x: number, y: number, extent: number,
  tileX: number, tileY: number, tileZ: number
): [number, number] {
  const n = 1 << tileZ;
  const lng = ((tileX + x / extent) / n) * 360 - 180;
  const latRad = Math.atan(Math.sinh(Math.PI * (1 - (2 * (tileY + y / extent)) / n)));
  const lat = (latRad * 180) / Math.PI;
  return [lng, lat];
}

// Fetch and decode a vector tile, extract named features
async function decodeTile(
  sourceId: string, z: number, x: number, y: number
): Promise<PlaceEntry[]> {
  const entries: PlaceEntry[] = [];
  try {
    const resp = await fetch(`${TILE_API}/${sourceId}/${z}/${x}/${y}`);
    if (!resp.ok) return entries;
    const buf = await resp.arrayBuffer();
    const tile = new VectorTile(new Pbf(buf));

    for (const layerName of ['places', 'pois', 'water']) {
      const layer = tile.layers[layerName];
      if (!layer) continue;

      for (let i = 0; i < layer.length; i++) {
        const feature = layer.feature(i);
        const name = feature.properties.name as string;
        if (!name) continue;

        // Get geometry center point
        const geom = feature.loadGeometry();
        if (!geom.length || !geom[0].length) continue;

        // Use first point for point features, midpoint for lines/polygons
        let px: number, py: number;
        if (geom[0].length === 1) {
          px = geom[0][0].x;
          py = geom[0][0].y;
        } else {
          const mid = geom[0][Math.floor(geom[0].length / 2)];
          px = mid.x;
          py = mid.y;
        }

        const coords = tileCoordsToLngLat(px, py, layer.extent, x, y, z);
        entries.push({
          name,
          kind: (feature.properties.kind as string) || layerName,
          coords,
        });
      }
    }
  } catch {
    // tile fetch or decode failed
  }
  return entries;
}

// Maximum number of tiles to fetch when pre-building the place index.
// Above this threshold the index pre-build is skipped entirely; search
// falls back to viewport tile features + Photon, which scale fine.
const PLACE_INDEX_TILE_BUDGET = 400;
// Concurrent in-flight tile fetches when we do build.
const PLACE_INDEX_CONCURRENCY = 8;

interface TileCoord { z: number; x: number; y: number; }

function tileRangeForBounds(
  z: number,
  west: number,
  south: number,
  east: number,
  north: number
): { xMin: number; xMax: number; yMin: number; yMax: number } {
  const n = 1 << z;
  const xMin = Math.max(0, Math.floor(((west + 180) / 360) * n));
  const xMax = Math.min(n - 1, Math.floor(((east + 180) / 360) * n));
  const yMinCalc = Math.floor(
    ((1 - Math.log(Math.tan((north * Math.PI) / 180) + 1 / Math.cos((north * Math.PI) / 180)) / Math.PI) / 2) * n
  );
  const yMaxCalc = Math.floor(
    ((1 - Math.log(Math.tan((south * Math.PI) / 180) + 1 / Math.cos((south * Math.PI) / 180)) / Math.PI) / 2) * n
  );
  return {
    xMin,
    xMax,
    yMin: Math.max(0, yMinCalc),
    yMax: Math.min(n - 1, yMaxCalc),
  };
}

// Run a list of async tasks with bounded concurrency.
async function runWithConcurrency<T>(
  tasks: Array<() => Promise<T>>,
  limit: number
): Promise<T[]> {
  const results: T[] = new Array(tasks.length);
  let next = 0;
  const workers: Promise<void>[] = [];
  for (let w = 0; w < Math.min(limit, tasks.length); w++) {
    workers.push(
      (async () => {
        while (true) {
          const i = next++;
          if (i >= tasks.length) return;
          results[i] = await tasks[i]();
        }
      })()
    );
  }
  await Promise.all(workers);
  return results;
}

// Build place index by fetching low-zoom tiles covering the source bounds.
// Skips entirely for global / planet-scale sources to avoid 70k tile fetches.
export async function buildPlaceIndex(_map: Map, sourceId: string) {
  try {
    const meta = await (await fetch(`${TILE_API}/${sourceId}`)).json();
    const bounds = meta.bounds || [-180, -85, 180, 85];
    const [west, south, east, north] = bounds;

    // Count candidate tiles across z4/z6/z8 and bail if it exceeds the budget.
    const zooms = [4, 6, 8];
    const ranges = zooms.map((z) => tileRangeForBounds(z, west, south, east, north));
    const totalTiles = ranges.reduce(
      (sum, r) => sum + (r.xMax - r.xMin + 1) * (r.yMax - r.yMin + 1),
      0
    );

    if (totalTiles > PLACE_INDEX_TILE_BUDGET) {
      console.log(
        `Place index pre-build skipped for "${sourceId}" (${totalTiles} tiles > ${PLACE_INDEX_TILE_BUDGET}); using viewport + Photon search.`
      );
      placeIndex = [];
      indexReady = true;
      return;
    }

    const coords: TileCoord[] = [];
    zooms.forEach((z, i) => {
      const r = ranges[i];
      for (let x = r.xMin; x <= r.xMax; x++) {
        for (let y = r.yMin; y <= r.yMax; y++) {
          coords.push({ z, x, y });
        }
      }
    });

    const tasks = coords.map((c) => () => decodeTile(sourceId, c.z, c.x, c.y));
    const results = await runWithConcurrency(tasks, PLACE_INDEX_CONCURRENCY);

    const seen = new Set<string>();
    const allEntries: PlaceEntry[] = [];
    results.forEach((entries) => {
      entries.forEach((e) => {
        const key = e.name.toLowerCase();
        if (!seen.has(key)) {
          seen.add(key);
          allEntries.push(e);
        }
      });
    });

    placeIndex = allEntries;
    indexReady = true;
    console.log(`Place index built: ${placeIndex.length} entries from ${coords.length} tiles`);
  } catch (err) {
    console.warn('Failed to build place index:', err);
  }
}

// Search the place index
function searchPlaceIndex(query: string): SearchResult[] {
  const q = query.toLowerCase();
  return placeIndex
    .filter((e) => e.name.toLowerCase().includes(q))
    .sort((a, b) => {
      const aPre = a.name.toLowerCase().startsWith(q) ? 0 : 1;
      const bPre = b.name.toLowerCase().startsWith(q) ? 0 : 1;
      return aPre !== bPre ? aPre - bPre : a.name.localeCompare(b.name);
    })
    .slice(0, 15)
    .map((e) => ({
      name: e.name,
      detail: e.kind,
      type: e.kind,
      coords: e.coords,
      source: 'index' as const,
    }));
}

// Search loaded vector tile features at current zoom
function searchTileFeatures(map: Map, query: string): SearchResult[] {
  const q = query.toLowerCase();
  const results: SearchResult[] = [];
  const seen = new Set<string>();

  ['places', 'pois', 'roads', 'water', 'earth', 'buildings'].forEach((sourceLayer) => {
    try {
      const features = map.querySourceFeatures('tiles', { sourceLayer });
      features.forEach((f) => {
        const name = (f.properties?.name as string) || '';
        if (!name || !name.toLowerCase().includes(q)) return;

        let coords: [number, number] | null = null;
        const geom = f.geometry;
        if (geom.type === 'Point') {
          coords = (geom as GeoJSON.Point).coordinates as [number, number];
        } else if (geom.type === 'LineString') {
          const line = (geom as GeoJSON.LineString).coordinates;
          const mid = line[Math.floor(line.length / 2)];
          coords = [mid[0], mid[1]];
        } else if (geom.type === 'Polygon') {
          const ring = (geom as GeoJSON.Polygon).coordinates[0];
          const mid = ring[Math.floor(ring.length / 2)];
          coords = [mid[0], mid[1]];
        }
        if (!coords) return;

        const key = `${name}|${coords[0].toFixed(3)}|${coords[1].toFixed(3)}`;
        if (seen.has(key)) return;
        seen.add(key);

        results.push({
          name,
          detail: (f.properties?.kind as string) || sourceLayer,
          type: sourceLayer,
          coords,
          source: 'map',
        });
      });
    } catch {
      // source layer may not exist
    }
  });

  results.sort((a, b) => {
    const aPre = a.name.toLowerCase().startsWith(q) ? 0 : 1;
    const bPre = b.name.toLowerCase().startsWith(q) ? 0 : 1;
    return aPre !== bPre ? aPre - bPre : a.name.localeCompare(b.name);
  });
  return results.slice(0, 20);
}

// Search via Photon geocoding
async function searchPhoton(query: string): Promise<SearchResult[]> {
  try {
    const resp = await fetch(`${PHOTON_URL}/api?q=${encodeURIComponent(query)}&limit=8`);
    const data = await resp.json();
    return (data.features || []).map((f: PhotonFeature) => {
      const p = f.properties || {};
      return {
        name: p.name || p.street || 'Unknown',
        detail: [p.city, p.state, p.country].filter(Boolean).join(', '),
        type: p.osm_value || p.type || '',
        coords: f.geometry.coordinates,
        source: 'geocoder' as const,
      };
    });
  } catch {
    return [];
  }
}

// Deduplicate
function deduplicateResults(results: SearchResult[]): SearchResult[] {
  const merged: SearchResult[] = [];
  results.forEach((r) => {
    const isDup = merged.some(
      (m) =>
        Math.abs(m.coords[0] - r.coords[0]) < 0.001 &&
        Math.abs(m.coords[1] - r.coords[1]) < 0.001 &&
        m.name.toLowerCase() === r.name.toLowerCase()
    );
    if (!isDup) merged.push(r);
  });
  return merged.slice(0, 12);
}

function createResultElement(result: SearchResult, onClick: () => void): HTMLElement {
  const el = document.createElement('div');
  el.className = 'search-result';

  const nameEl = document.createElement('div');
  nameEl.className = 'name';
  nameEl.textContent = result.name;

  const badgeColors: Record<string, string> = {
    map: 'background:#1a3a2a;color:#3fb950',
    index: 'background:#1a2a3a;color:#58a6ff',
    geocoder: 'background:#2a1a3a;color:#bc8cff',
  };
  const badge = document.createElement('span');
  badge.style.cssText =
    'font-size:9px;padding:1px 5px;border-radius:3px;margin-left:6px;vertical-align:middle;letter-spacing:0.3px;' +
    (badgeColors[result.source] || '');
  badge.textContent = result.source === 'map' ? 'tile' : result.source === 'index' ? 'index' : 'geo';
  nameEl.appendChild(badge);

  if (result.type) {
    const typeSpan = document.createElement('span');
    typeSpan.style.cssText = 'color:var(--text-faint);font-weight:normal;font-size:11px;margin-left:6px';
    typeSpan.textContent = result.type;
    nameEl.appendChild(typeSpan);
  }
  el.appendChild(nameEl);

  if (result.detail) {
    const detailEl = document.createElement('div');
    detailEl.className = 'detail';
    detailEl.textContent = result.detail;
    el.appendChild(detailEl);
  }

  el.addEventListener('click', onClick);
  return el;
}

export function setupSearch(map: Map) {
  const input = document.getElementById('search') as HTMLInputElement;
  const resultsEl = document.getElementById('results')!;
  let debounce: ReturnType<typeof setTimeout> | null = null;

  async function doSearch(query: string) {
    while (resultsEl.firstChild) resultsEl.removeChild(resultsEl.firstChild);

    const [indexResults, tileResults, photonResults] = await Promise.all([
      Promise.resolve(searchPlaceIndex(query)),
      Promise.resolve(searchTileFeatures(map, query)),
      searchPhoton(query),
    ]);

    const all = [...indexResults, ...tileResults, ...photonResults];
    const merged = deduplicateResults(all);

    if (merged.length === 0) {
      const noResult = document.createElement('div');
      noResult.className = 'search-result';
      const noDetail = document.createElement('div');
      noDetail.className = 'detail';
      noDetail.textContent = indexReady ? 'No results found' : 'Building place index...';
      noResult.appendChild(noDetail);
      resultsEl.appendChild(noResult);
      resultsEl.classList.add('visible');
      return;
    }

    merged.forEach((result) => {
      const el = createResultElement(result, () => {
        map.flyTo({ center: result.coords, zoom: 14, duration: 1500 });
        const popupDiv = document.createElement('div');
        popupDiv.style.cssText = 'font-size:13px;padding:2px';
        const strong = document.createElement('strong');
        strong.textContent = result.name;
        popupDiv.appendChild(strong);
        if (result.detail) {
          const detail = document.createElement('div');
          detail.style.cssText = 'font-size:11px;color:var(--text-muted);margin-top:2px';
          detail.textContent = result.detail;
          popupDiv.appendChild(detail);
        }
        new Popup({ closeButton: true, maxWidth: '280px' })
          .setLngLat(result.coords)
          .setDOMContent(popupDiv)
          .addTo(map);
        resultsEl.classList.remove('visible');
        input.value = result.name;
      });
      resultsEl.appendChild(el);
    });

    resultsEl.classList.add('visible');
  }

  input.addEventListener('input', () => {
    if (debounce) clearTimeout(debounce);
    const q = input.value.trim();
    if (q.length < 2) {
      resultsEl.classList.remove('visible');
      return;
    }
    debounce = setTimeout(() => doSearch(q), 300);
  });

  input.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      resultsEl.classList.remove('visible');
      input.blur();
    }
  });

  document.addEventListener('click', (e) => {
    if (!(e.target as Element).closest('.search-container, .sidebar-search')) {
      resultsEl.classList.remove('visible');
    }
  });
}
