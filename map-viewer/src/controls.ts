import { Map, Popup } from 'maplibre-gl';

export function setupCoords(map: Map) {
  const coordsEl = document.getElementById('coords')!;
  const zoomEl = document.getElementById('zoom-level')!;

  map.on('mousemove', (e) => {
    coordsEl.textContent = `${e.lngLat.lat.toFixed(5)}°, ${e.lngLat.lng.toFixed(5)}°`;
  });

  const updateZoom = () => {
    zoomEl.textContent = `z${map.getZoom().toFixed(1)}`;
  };
  map.on('zoom', updateZoom);
  map.on('load', updateZoom);

  map.on('click', (e) => {
    // Don't show coord popup when measuring
    if (map.getCanvas().style.cursor === 'crosshair') return;
    const lat = e.lngLat.lat.toFixed(6);
    const lng = e.lngLat.lng.toFixed(6);
    const coordDiv = document.createElement('div');
    coordDiv.style.cssText = 'font-family:monospace;font-size:13px;padding:4px;color:#e0e0e0';
    coordDiv.textContent = `${lat}°, ${lng}°`;
    new Popup({ closeButton: true, maxWidth: '260px', className: 'dark-popup' })
      .setLngLat(e.lngLat)
      .setDOMContent(coordDiv)
      .addTo(map);
  });
}
