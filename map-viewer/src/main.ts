import '@fontsource/dm-sans/400.css';
import '@fontsource/dm-sans/500.css';
import '@fontsource/dm-sans/600.css';
import '@fontsource/dm-sans/700.css';
import '@fontsource/jetbrains-mono/400.css';
import '@fontsource/jetbrains-mono/500.css';
import 'maplibre-gl/dist/maplibre-gl.css';
import { initMap } from './map';
import { setupSearch, buildPlaceIndex } from './search';
import { setupLayerSwitcher } from './layers';
import { setupCoords } from './controls';
import { setupMeasure } from './measure';
import { setupSidebar } from './sidebar';
import './app.css';

async function init() {
  setupSidebar();

  const map = await initMap();
  if (!map) return;

  setupCoords(map);
  setupLayerSwitcher(map);
  setupMeasure(map);
  setupSearch(map);

  // Build offline place index from low-zoom tiles once map is loaded
  map.on('load', async () => {
    try {
      const catalog = await (await fetch(window.location.origin + '/catalog')).json();
      const baseSource = Object.keys(catalog.tiles || {}).find(
        (k: string) => !k.startsWith('terrain') && !k.startsWith('nautical')
      );
      if (baseSource) {
        buildPlaceIndex(map, baseSource);
      }
    } catch (e) {
      console.warn('Failed to build place index:', e);
    }
  });
}

init();
