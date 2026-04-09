import maplibregl, { Map, NavigationControl, ScaleControl, GeolocateControl, AttributionControl } from 'maplibre-gl';
import mlcontour from 'maplibre-contour';
import { buildStyle, getThemes, isRasterTheme, type ThemeId, type OverlaySources } from './styles';
import { getUnits, onUnitsChange } from './units';

const TILE_API = window.location.origin;
const BASE_SOURCE_KEY = 'krull-map-base-source';

let currentTheme: ThemeId = 'light';
let currentSource: string | null = null;
let currentOverlays: OverlaySources = {};
let terrainEnabled = false;
let availableBaseSources: string[] = [];

export { currentTheme, currentSource };
export { getThemes, isRasterTheme };
export type { ThemeId } from './styles';

export function getBaseSources(): string[] {
  return availableBaseSources.slice();
}

export function getCurrentBaseSource(): string | null {
  return currentSource;
}

export function setBaseSource(map: Map, sourceId: string) {
  if (!availableBaseSources.includes(sourceId)) return;
  currentSource = sourceId;
  try {
    localStorage.setItem(BASE_SOURCE_KEY, sourceId);
  } catch {
    // ignore quota / unavailable
  }
  map.setStyle(buildStyle(currentSource, currentTheme, currentOverlays));
  map.once('style.load', () => {
    if (!isRasterTheme(currentTheme)) {
      setupContours(map);
      if (terrainEnabled && currentOverlays.terrainSourceId) {
        map.setTerrain({ source: 'terrain-dem', exaggeration: 1.2 });
      }
    }
  });
}

export function setTheme(map: Map, theme: ThemeId) {
  currentTheme = theme;
  if (currentSource) {
    map.setStyle(buildStyle(currentSource, currentTheme, currentOverlays));
    // Re-add contour source after style change (only for vector themes with terrain)
    map.once('style.load', () => {
      if (!isRasterTheme(currentTheme)) {
        setupContours(map);
        if (terrainEnabled && currentOverlays.terrainSourceId) {
          map.setTerrain({ source: 'terrain-dem', exaggeration: 1.2 });
        }
      }
    });
  }
}

export function toggleTerrain(map: Map) {
  if (!currentOverlays.terrainSourceId) return false;
  terrainEnabled = !terrainEnabled;
  if (terrainEnabled) {
    map.setTerrain({ source: 'terrain-dem', exaggeration: 1.5 });
    if (map.getPitch() < 30) {
      map.easeTo({ pitch: 50, duration: 800 });
    }
  } else {
    map.setTerrain(undefined as unknown as Parameters<Map['setTerrain']>[0]);
    map.easeTo({ pitch: 0, duration: 800 });
  }
  return terrainEnabled;
}

export function hasTerrainSource(): boolean {
  return !!currentOverlays.terrainSourceId;
}

export function hasNauticalSource(): boolean {
  return !!currentOverlays.nauticalSourceIds && currentOverlays.nauticalSourceIds.length > 0;
}

export function hasAeroSource(): boolean {
  return !!currentOverlays.aeroSourceIds && currentOverlays.aeroSourceIds.length > 0;
}

interface CatalogEntry {
  content_type?: string;
}

interface Catalog {
  tiles?: Record<string, CatalogEntry>;
}

async function getTileSources(): Promise<{
  baseSources: string[];
  terrainSource?: string;
  nauticalSources: string[];
  aeroSources: string[];
}> {
  try {
    const resp = await fetch(`${TILE_API}/catalog`);
    const catalog: Catalog = await resp.json();
    const all = Object.keys(catalog.tiles || {});

    const baseSources: string[] = [];
    let terrainSource: string | undefined;
    const nauticalSources: string[] = [];
    const aeroSources: string[] = [];

    all.forEach((name) => {
      if (name.startsWith('terrain')) {
        terrainSource = name;
      } else if (name.startsWith('nautical')) {
        nauticalSources.push(name);
      } else if (name.startsWith('aero')) {
        aeroSources.push(name);
      } else {
        baseSources.push(name);
      }
    });

    return { baseSources, terrainSource, nauticalSources, aeroSources };
  } catch {
    return { baseSources: [], nauticalSources: [], aeroSources: [] };
  }
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
let demSource: any = null;

function setupContours(map: Map) {
  if (!currentOverlays.terrainSourceId || !demSource) return;

  if (!map.getSource('contour-source')) {
    map.addSource('contour-source', {
      type: 'vector',
      tiles: [
        demSource.contourProtocolUrl({
          multiplier: 3.28084,
          thresholds: {
            11: [200, 1000],
            12: [100, 500],
            13: [50, 200],
            14: [50, 200],
            15: [20, 100],
          },
          contourLayer: 'contours',
          elevationKey: 'ele',
          levelKey: 'level',
          extent: 4096,
          buffer: 1,
        }),
      ],
      maxzoom: 15,
    });
  }

  if (!map.getLayer('contour-lines')) {
    map.addLayer({
      id: 'contour-lines',
      type: 'line',
      source: 'contour-source',
      'source-layer': 'contours',
      paint: {
        'line-color': 'rgba(139, 90, 43, 0.4)',
        'line-width': ['match', ['get', 'level'], 1, 1, 0.5],
      },
    });
  }

  if (!map.getLayer('contour-labels')) {
    map.addLayer({
      id: 'contour-labels',
      type: 'symbol',
      source: 'contour-source',
      'source-layer': 'contours',
      filter: ['>', ['get', 'level'], 0],
      layout: {
        'symbol-placement': 'line',
        'text-size': 10,
        'text-field': ['concat', ['number-format', ['get', 'ele'], {}], "'"],
        'text-font': ['Noto Sans Regular'],
      },
      paint: {
        'text-color': 'rgba(139, 90, 43, 0.7)',
        'text-halo-color': 'white',
        'text-halo-width': 1,
      },
    });
  }
}

interface SourceMeta {
  bounds?: [number, number, number, number];
  center?: [number, number, number?];
}

async function fetchSourceMeta(sourceId: string): Promise<SourceMeta | null> {
  try {
    return (await (await fetch(`${TILE_API}/${sourceId}`)).json()) as SourceMeta;
  } catch {
    return null;
  }
}

function bboxArea(bounds?: [number, number, number, number]): number {
  if (!bounds) return 0;
  const [w, s, e, n] = bounds;
  return Math.max(0, e - w) * Math.max(0, n - s);
}

// A source whose bounds cover most of the planet (≥ ~50% of full 360x170).
function isGlobalBounds(bounds?: [number, number, number, number]): boolean {
  return bboxArea(bounds) >= (360 * 170) * 0.5;
}

export async function initMap(): Promise<Map | null> {
  const { baseSources, terrainSource, nauticalSources, aeroSources } = await getTileSources();
  if (baseSources.length === 0) {
    const mapEl = document.getElementById('map');
    if (mapEl) {
      const msg = document.createElement('div');
      msg.style.cssText =
        'display:flex;align-items:center;justify-content:center;height:100%;font-size:18px;color:#666;text-align:center;padding:20px';
      msg.textContent = 'No tile sources found. Download maps first: ./krull download-maps oregon';
      mapEl.appendChild(msg);
    }
    return null;
  }

  availableBaseSources = baseSources.slice();

  // Fetch metadata for all base sources so we can rank by coverage and read center.
  const baseMetas = await Promise.all(baseSources.map((id) => fetchSourceMeta(id)));
  const metaById: Record<string, SourceMeta | null> = {};
  baseSources.forEach((id, i) => {
    metaById[id] = baseMetas[i];
  });

  // Restore saved choice if it still exists; otherwise prefer the source with the
  // largest bbox (planet wins over regional). Falls back to first if no metadata.
  let saved: string | null = null;
  try {
    saved = localStorage.getItem(BASE_SOURCE_KEY);
  } catch {
    // ignore
  }
  if (saved && baseSources.includes(saved)) {
    currentSource = saved;
  } else {
    let best = baseSources[0];
    let bestArea = bboxArea(metaById[best]?.bounds);
    for (const id of baseSources.slice(1)) {
      const area = bboxArea(metaById[id]?.bounds);
      if (area > bestArea) {
        best = id;
        bestArea = area;
      }
    }
    currentSource = best;
  }

  currentOverlays = {
    terrainSourceId: terrainSource,
    nauticalSourceIds: nauticalSources,
    aeroSourceIds: aeroSources,
  };

  // Set up maplibre-contour DEM source if terrain tiles available
  if (terrainSource) {
    demSource = new mlcontour.DemSource({
      url: `${TILE_API}/${terrainSource}/{z}/{x}/{y}`,
      encoding: 'terrarium',
      maxzoom: 9,
      worker: true,
      cacheSize: 100,
      timeoutMs: 10000,
    });
    demSource.setupMaplibre(maplibregl);
  }

  // Pick initial center/zoom from the active source's metadata, with a global default.
  const meta = metaById[currentSource] || null;
  const globalSource = isGlobalBounds(meta?.bounds);
  let center: [number, number] = [0, 20];
  let zoom = globalSource ? 2 : 4;
  if (meta?.center) {
    center = [meta.center[0], meta.center[1]];
    if (!globalSource && typeof meta.center[2] === 'number') {
      zoom = meta.center[2];
    }
  }

  // Check URL hash for saved position
  if (window.location.hash) {
    const parts = window.location.hash.replace('#', '').split('/');
    if (parts.length >= 3) {
      zoom = parseFloat(parts[0]);
      center = [parseFloat(parts[2]), parseFloat(parts[1])];
    }
  }

  const style = buildStyle(currentSource, currentTheme, currentOverlays);
  const map = new maplibregl.Map({
    container: 'map',
    style,
    center,
    zoom,
    hash: true,
    attributionControl: false,
  });

  // Add attribution — always visible, no toggle button
  map.addControl(new AttributionControl({ compact: false }), 'bottom-right');

  // Expose map instance for search and debugging
  (window as unknown as Record<string, unknown>).__krullMap = map;

  map.addControl(new NavigationControl(), 'bottom-right');
  const scaleControl = new ScaleControl({ unit: getUnits() });
  map.addControl(scaleControl, 'bottom-right');

  // Update scale bar when units change
  onUnitsChange((u) => {
    scaleControl.setUnit(u);
  });
  map.addControl(
    new GeolocateControl({
      positionOptions: { enableHighAccuracy: true },
      trackUserLocation: true,
    }),
    'bottom-right'
  );

  // Set up contour lines once map loads (only for vector themes)
  if (terrainSource) {
    map.on('load', () => {
      if (!isRasterTheme(currentTheme)) {
        setupContours(map);
      }
    });
  }

  return map;
}
