import maplibregl, { Map, NavigationControl, ScaleControl, GeolocateControl, AttributionControl } from 'maplibre-gl';
import mlcontour from 'maplibre-contour';
import { buildStyle, getThemes, isRasterTheme, type ThemeId, type OverlaySources } from './styles';
import { getUnits, onUnitsChange } from './units';

const TILE_API = window.location.origin;

let currentTheme: ThemeId = 'light';
let currentSource: string | null = null;
let currentOverlays: OverlaySources = {};
let terrainEnabled = false;

export { currentTheme, currentSource };
export { getThemes, isRasterTheme };
export type { ThemeId } from './styles';

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

  currentSource = baseSources[0];
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

  // Get center from tile metadata
  let center: [number, number] = [-122.68, 45.52];
  let zoom = 11;
  try {
    const meta = await (await fetch(`${TILE_API}/${currentSource}`)).json();
    if (meta.center) {
      center = [meta.center[0], meta.center[1]];
      zoom = meta.center[2] || zoom;
    }
  } catch {
    // use defaults
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
