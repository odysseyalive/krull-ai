import { layers, namedFlavor } from '@protomaps/basemaps';
import type { LayerSpecification, StyleSpecification } from 'maplibre-gl';

const TILE_API = window.location.origin;

// Protomaps vector themes
type VectorThemeId = 'light' | 'dark' | 'white' | 'black' | 'grayscale';

// Raster chart themes — rendered as the base map, with vector labels on top
type RasterThemeId = 'nautical' | 'aeronautical';

export type ThemeId = VectorThemeId | RasterThemeId;

export function isRasterTheme(theme: ThemeId): boolean {
  return theme === 'nautical' || theme === 'aeronautical';
}

export interface ThemeEntry {
  id: ThemeId;
  label: string;
  available: boolean; // set dynamically based on downloaded data
}

// Base themes always available; raster themes depend on downloaded chart data
export function getThemes(hasNautical: boolean, hasAero: boolean): ThemeEntry[] {
  return [
    { id: 'light', label: 'Light', available: true },
    { id: 'dark', label: 'Dark', available: true },
    { id: 'white', label: 'White', available: true },
    { id: 'black', label: 'Black', available: true },
    { id: 'grayscale', label: 'Grayscale', available: true },
    { id: 'nautical', label: 'Nautical', available: hasNautical },
    { id: 'aeronautical', label: 'Aeronautical', available: hasAero },
  ];
}

// Label layer groups for toggle controls
export const LABEL_GROUPS: Record<string, string[]> = {
  'Place names': ['places_country', 'places_region', 'places_locality', 'places_subplace'],
  'Road names': ['roads_labels_major', 'roads_labels_minor', 'roads_shields'],
  'Water names': ['water_label_ocean', 'water_label_lakes', 'water_waterway_label'],
  'POI labels': ['pois'],
  'Other labels': ['address_label', 'earth_label_islands'],
};

// Overlay layer groups for toggle controls (only relevant for vector themes)
export const OVERLAY_GROUPS: Record<string, string[]> = {
  Hillshade: ['hillshade-layer'],
  'Contour lines': ['contour-lines', 'contour-labels'],
};

// Zoom overrides for labels that appear too late or have no size interpolation
interface LabelOverride {
  minzoom?: number;
  textSize?: unknown;
  haloWidth?: number;
}

const LABEL_OVERRIDES: Record<string, LabelOverride> = {
  roads_labels_major: {
    minzoom: 8,
    textSize: ['interpolate', ['linear'], ['zoom'], 8, 9, 12, 12, 16, 14],
  },
  roads_labels_minor: {
    minzoom: 12,
    textSize: ['interpolate', ['linear'], ['zoom'], 12, 8, 15, 11, 18, 14],
  },
  water_waterway_label: {
    minzoom: 10,
    textSize: ['interpolate', ['linear'], ['zoom'], 10, 9, 13, 12, 16, 14],
  },
  address_label: {
    minzoom: 16,
    textSize: ['interpolate', ['linear'], ['zoom'], 16, 9, 18, 12],
  },
};

function patchLabelLayers(styleLayers: LayerSpecification[], isDark: boolean): LayerSpecification[] {
  return styleLayers.map((layer) => {
    const override = LABEL_OVERRIDES[layer.id];
    if (!override) return layer;

    const patched = { ...layer } as Record<string, unknown>;

    if (override.minzoom !== undefined) {
      patched.minzoom = override.minzoom;
    }

    if (override.textSize) {
      patched.layout = {
        ...((layer as Record<string, unknown>).layout as object),
        'text-size': override.textSize,
      };
    }

    const existingPaint = (layer as Record<string, unknown>).paint as Record<string, unknown> | undefined;
    if (existingPaint) {
      patched.paint = {
        ...existingPaint,
        'text-halo-width': override.haloWidth ?? (isDark ? 2 : 1.5),
      };
    }

    return patched as LayerSpecification;
  });
}

export interface OverlaySources {
  terrainSourceId?: string;
  nauticalSourceIds?: string[];
  aeroSourceIds?: string[];
}

/**
 * Build a raster chart style: the chart tiles render as the base map,
 * with protomaps vector labels/roads/line features overlaid on top.
 */
function buildRasterChartStyle(
  vectorSourceId: string,
  rasterSourceIds: string[],
  attribution: string,
  maxzoom: number
): StyleSpecification {
  const sources: StyleSpecification['sources'] = {
    tiles: {
      type: 'vector',
      tiles: [`${TILE_API}/${vectorSourceId}/{z}/{x}/{y}`],
      maxzoom: 15,
      attribution:
        '<a href="https://protomaps.com">Protomaps</a> &copy; <a href="https://openstreetmap.org">OpenStreetMap</a>',
    },
  };

  // Add raster chart sources
  const rasterLayers: LayerSpecification[] = [];
  rasterSourceIds.forEach((srcId, i) => {
    const sourceKey = `chart-${i}`;
    sources[sourceKey] = {
      type: 'raster',
      tiles: [`${TILE_API}/${srcId}/{z}/{x}/{y}`],
      tileSize: 256,
      maxzoom,
      attribution: i === 0 ? attribution : '',
    } as StyleSpecification['sources'][string];

    rasterLayers.push({
      id: `chart-base-${i}`,
      type: 'raster',
      source: sourceKey,
      paint: { 'raster-opacity': 1 },
    } as LayerSpecification);
  });

  // Background layer so the canvas isn't transparent while raster tiles load
  const bgLayer: LayerSpecification = {
    id: 'chart-background',
    type: 'background',
    paint: { 'background-color': '#f0e6c8' }, // NOAA chart land color
  } as LayerSpecification;

  // Get vector label/line layers from protomaps (light theme for readability on charts)
  const flavor = namedFlavor('light');
  let vectorLayers = layers('tiles', flavor, { lang: 'en' }) as LayerSpecification[];
  vectorLayers = patchLabelLayers(vectorLayers, false);

  // Only keep symbol (label) and line (road) layers — skip fill layers
  // since the raster chart provides its own land/water rendering.
  const overlayLayers = vectorLayers.filter(
    (l) => l.type === 'symbol' || l.type === 'line'
  );

  return {
    version: 8,
    glyphs: `${TILE_API}/fonts/{fontstack}/{range}.pbf`,
    sprite: `${TILE_API}/sprites/light`,
    sources,
    layers: [bgLayer, ...rasterLayers, ...overlayLayers],
  };
}

/**
 * Build a vector (protomaps) style with optional terrain hillshade overlay.
 */
function buildVectorStyle(
  sourceId: string,
  theme: VectorThemeId,
  overlays?: OverlaySources
): StyleSpecification {
  const flavor = namedFlavor(theme);
  const isDark = theme === 'dark' || theme === 'black';
  let styleLayers = layers('tiles', flavor, { lang: 'en' }) as LayerSpecification[];

  styleLayers = patchLabelLayers(styleLayers, isDark);

  const sources: StyleSpecification['sources'] = {
    tiles: {
      type: 'vector',
      tiles: [`${TILE_API}/${sourceId}/{z}/{x}/{y}`],
      maxzoom: 15,
      attribution:
        '<a href="https://protomaps.com">Protomaps</a> &copy; <a href="https://openstreetmap.org">OpenStreetMap</a>',
    },
  };

  // Insert terrain hillshade before labels
  const firstSymbolIdx = styleLayers.findIndex((l) => l.type === 'symbol');
  const insertIdx = firstSymbolIdx >= 0 ? firstSymbolIdx : styleLayers.length;

  if (overlays?.terrainSourceId) {
    sources['terrain-dem'] = {
      type: 'raster-dem',
      tiles: [`${TILE_API}/${overlays.terrainSourceId}/{z}/{x}/{y}`],
      tileSize: 256,
      encoding: 'terrarium',
      maxzoom: 9,
    } as StyleSpecification['sources'][string];

    styleLayers.splice(insertIdx, 0, {
      id: 'hillshade-layer',
      type: 'hillshade',
      source: 'terrain-dem',
      paint: {
        'hillshade-shadow-color': isDark ? '#000000' : '#3d3d3d',
        'hillshade-highlight-color': isDark ? '#444444' : '#ffffff',
        'hillshade-accent-color': isDark ? '#111111' : '#d4d4d4',
        'hillshade-illumination-anchor': 'viewport',
        'hillshade-exaggeration': 0.3,
      },
    } as LayerSpecification);
  }

  return {
    version: 8,
    glyphs: `${TILE_API}/fonts/{fontstack}/{range}.pbf`,
    sprite: `${TILE_API}/sprites/${theme}`,
    sources,
    layers: styleLayers,
  };
}

export function buildStyle(
  sourceId: string,
  theme: ThemeId,
  overlays?: OverlaySources
): StyleSpecification {
  if (theme === 'nautical' && overlays?.nauticalSourceIds?.length) {
    return buildRasterChartStyle(
      sourceId,
      overlays.nauticalSourceIds,
      '&copy; <a href="https://nauticalcharts.noaa.gov">NOAA</a>',
      15
    );
  }

  if (theme === 'aeronautical' && overlays?.aeroSourceIds?.length) {
    return buildRasterChartStyle(
      sourceId,
      overlays.aeroSourceIds,
      '&copy; <a href="https://www.faa.gov">FAA</a>',
      11
    );
  }

  // Fall back to light if a raster theme was selected but no data available
  const vectorTheme = (isRasterTheme(theme) ? 'light' : theme) as VectorThemeId;
  return buildVectorStyle(sourceId, vectorTheme, overlays);
}
