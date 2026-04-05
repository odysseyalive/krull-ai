import maplibregl, { Map, Marker, Popup, LngLat } from 'maplibre-gl';

import { getUnits, onUnitsChange } from './units';

const SOURCE_ID = 'measure-line';
const LAYER_ID = 'measure-line-layer';

let active = false;
let points: LngLat[] = [];
let markers: Marker[] = [];
let popup: Popup | null = null;
let currentMap: Map | null = null;
let updatingPopup = false; // flag to prevent close handler during programmatic updates

function haversineDistance(a: LngLat, b: LngLat): number {
  const R = 6371; // km
  const toRad = (d: number) => (d * Math.PI) / 180;
  const dLat = toRad(b.lat - a.lat);
  const dLon = toRad(b.lng - a.lng);
  const sinLat = Math.sin(dLat / 2);
  const sinLon = Math.sin(dLon / 2);
  const h = sinLat * sinLat + Math.cos(toRad(a.lat)) * Math.cos(toRad(b.lat)) * sinLon * sinLon;
  return 2 * R * Math.asin(Math.sqrt(h));
}

function formatDistance(km: number): string {
  if (getUnits() === 'metric') {
    return km < 1 ? `${(km * 1000).toFixed(0)} m` : `${km.toFixed(2)} km`;
  }
  const mi = km * 0.621371;
  return mi < 0.1 ? `${(mi * 5280).toFixed(0)} ft` : `${mi.toFixed(2)} mi`;
}

function totalDistance(): number {
  let km = 0;
  for (let i = 1; i < points.length; i++) {
    km += haversineDistance(points[i - 1], points[i]);
  }
  return km;
}

function clearMeasurement(map: Map) {
  markers.forEach((m) => m.remove());
  markers = [];
  points = [];
  if (popup) {
    updatingPopup = true;
    popup.remove();
    updatingPopup = false;
    popup = null;
  }
  if (map.getLayer(LAYER_ID)) map.removeLayer(LAYER_ID);
  if (map.getSource(SOURCE_ID)) map.removeSource(SOURCE_ID);
}

function updateLine(map: Map) {
  const coords = points.map((p) => [p.lng, p.lat]);
  const data = {
    type: 'Feature' as const,
    properties: {},
    geometry: { type: 'LineString' as const, coordinates: coords },
  };

  if (map.getSource(SOURCE_ID)) {
    (map.getSource(SOURCE_ID) as maplibregl.GeoJSONSource).setData(data);
  } else {
    map.addSource(SOURCE_ID, { type: 'geojson', data });
    map.addLayer({
      id: LAYER_ID,
      type: 'line',
      source: SOURCE_ID,
      paint: {
        'line-color': '#ff4444',
        'line-width': 2.5,
        'line-dasharray': [3, 2],
      },
    });
  }
}

function createMarker(lngLat: LngLat): Marker {
  const el = document.createElement('div');
  el.className = 'measure-point';
  return new Marker({ element: el }).setLngLat(lngLat);
}

function buildPopupContent(): HTMLElement {
  const km = totalDistance();
  const div = document.createElement('div');
  div.className = 'measure-popup';

  const dist = document.createElement('div');
  dist.className = 'measure-distance';
  dist.textContent = formatDistance(km);
  div.appendChild(dist);

  if (points.length > 2) {
    const segs = document.createElement('div');
    segs.className = 'measure-segments';
    segs.textContent = `${points.length - 1} segments`;
    div.appendChild(segs);
  }

  return div;
}

function showDistance(map: Map) {
  if (points.length < 2) return;

  const midIdx = Math.floor(points.length / 2);
  const midPoint =
    points.length === 2
      ? new LngLat((points[0].lng + points[1].lng) / 2, (points[0].lat + points[1].lat) / 2)
      : points[midIdx];

  // Remove old popup without triggering the close handler
  if (popup) {
    updatingPopup = true;
    popup.remove();
    updatingPopup = false;
    popup = null;
  }

  popup = new Popup({ closeButton: true, closeOnClick: false, anchor: 'bottom', offset: 12 })
    .setLngLat(midPoint)
    .setDOMContent(buildPopupContent())
    .addTo(map);

  // When the user clicks X, clear the measurement and deactivate
  popup.on('close', () => {
    if (updatingPopup) return;
    clearMeasurement(map);
    active = false;
    map.getCanvas().style.cursor = '';
    if (mapClickHandler) { map.off('click', mapClickHandler); mapClickHandler = null; }
    const btn = map.getContainer().querySelector('.measure-btn');
    if (btn) btn.classList.remove('active');
  });
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
let mapClickHandler: ((e: any) => void) | null = null;

function onMapClick(map: Map, e: { lngLat: LngLat; originalEvent: MouseEvent }) {
  e.originalEvent.stopPropagation();

  points.push(e.lngLat);
  const marker = createMarker(e.lngLat);
  marker.addTo(map);
  markers.push(marker);

  if (points.length >= 2) {
    updateLine(map);
    showDistance(map);
  }
}

export function setupMeasure(map: Map) {
  const btn = document.createElement('button');
  btn.className = 'measure-btn';
  btn.title = 'Measure distance';

  // Ruler icon
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('width', '16');
  svg.setAttribute('height', '16');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('fill', 'none');
  svg.setAttribute('stroke', 'currentColor');
  svg.setAttribute('stroke-width', '2');
  svg.setAttribute('stroke-linecap', 'round');
  const path1 = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  path1.setAttribute('d', 'M2 22 22 2');
  const path2 = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  path2.setAttribute('d', 'M6 18 8 16');
  const path3 = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  path3.setAttribute('d', 'M10 14 12 12');
  const path4 = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  path4.setAttribute('d', 'M14 10 16 8');
  svg.append(path1, path2, path3, path4);
  btn.appendChild(svg);

  const controlContainer = document.createElement('div');
  controlContainer.className = 'maplibregl-ctrl maplibregl-ctrl-group measure-ctrl';
  controlContainer.appendChild(btn);

  const bottomRight = map.getContainer().querySelector('.maplibregl-ctrl-bottom-right');
  if (bottomRight) {
    bottomRight.insertBefore(controlContainer, bottomRight.firstChild);
  }

  btn.addEventListener('click', () => {
    if (active) {
      clearMeasurement(map);
      active = false;
      btn.classList.remove('active');
      map.getCanvas().style.cursor = '';
      if (mapClickHandler) { map.off('click', mapClickHandler); mapClickHandler = null; }
      return;
    }

    clearMeasurement(map);
    active = true;
    btn.classList.add('active');
    map.getCanvas().style.cursor = 'crosshair';

    mapClickHandler = (e) => onMapClick(map, e);
    map.on('click', mapClickHandler);
  });

  currentMap = map;

  // Re-render popup when units change from the sidebar toggle
  onUnitsChange(() => {
    if (points.length >= 2 && currentMap) {
      showDistance(currentMap);
    }
  });
}
