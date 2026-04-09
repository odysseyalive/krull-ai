import type { Map } from 'maplibre-gl';
import {
  getThemes,
  setTheme,
  currentTheme,
  toggleTerrain,
  hasTerrainSource,
  hasNauticalSource,
  hasAeroSource,
  isRasterTheme,
  getBaseSources,
  getCurrentBaseSource,
  setBaseSource,
  type ThemeId,
} from './map';
import { LABEL_GROUPS, OVERLAY_GROUPS } from './styles';
import { getUnits, setUnits } from './units';

const STORAGE_KEY = 'krull-map-toggles';

function loadToggles(): Record<string, boolean> {
  try {
    return JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}');
  } catch {
    return {};
  }
}

function saveToggles(toggles: Record<string, boolean>) {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(toggles));
}

function setLayerVisibility(map: Map, layerIds: string[], visible: boolean) {
  layerIds.forEach((id) => {
    if (map.getLayer(id)) {
      map.setLayoutProperty(id, 'visibility', visible ? 'visible' : 'none');
    }
  });
}

function addToggle(
  container: HTMLElement,
  label: string,
  defaultOn: boolean,
  toggles: Record<string, boolean>,
  onChange: (checked: boolean) => void
): boolean {
  const isVisible = toggles[label] !== undefined ? toggles[label] : defaultOn;

  const div = document.createElement('div');
  div.className = 'toggle-option';

  const checkbox = document.createElement('input');
  checkbox.type = 'checkbox';
  checkbox.checked = isVisible;
  checkbox.id = `toggle-${label.replace(/\s/g, '-').toLowerCase()}`;

  const span = document.createElement('label');
  span.textContent = label;
  span.htmlFor = checkbox.id;
  span.style.cursor = 'pointer';

  div.appendChild(checkbox);
  div.appendChild(span);

  checkbox.addEventListener('change', () => {
    toggles[label] = checkbox.checked;
    saveToggles(toggles);
    onChange(checkbox.checked);
  });

  container.appendChild(div);
  return isVisible;
}

function prettyBaseLabel(id: string): string {
  // 'oregon' -> 'Oregon', 'us-west' -> 'Us West', 'planet' -> 'Planet'
  return id
    .replace(/[-_]+/g, ' ')
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

function setupBaseSourceSwitcher(map: Map) {
  const baseSources = getBaseSources();
  if (baseSources.length < 2) return; // hide when only one base map installed

  const sidebar = document.getElementById('sidebar')!;
  const styleSection = document.querySelector('.sidebar-section') as HTMLElement | null;

  const section = document.createElement('div');
  section.className = 'sidebar-section';

  const header = document.createElement('div');
  header.className = 'section-header';
  header.textContent = 'Base Map';
  section.appendChild(header);

  const body = document.createElement('div');
  body.className = 'section-body';

  const current = getCurrentBaseSource();
  baseSources.forEach((id) => {
    const opt = document.createElement('div');
    opt.className = 'style-option' + (id === current ? ' active' : '');
    opt.textContent = prettyBaseLabel(id);
    opt.addEventListener('click', () => {
      setBaseSource(map, id);
      body.querySelectorAll('.style-option').forEach((el) => el.classList.remove('active'));
      opt.classList.add('active');
    });
    body.appendChild(opt);
  });

  section.appendChild(body);

  // Insert above the existing Map Style section if present, else append.
  if (styleSection && styleSection.parentNode === sidebar) {
    sidebar.insertBefore(section, styleSection);
  } else {
    sidebar.appendChild(section);
  }
}

export function setupLayerSwitcher(map: Map) {
  setupBaseSourceSwitcher(map);

  const styleOptions = document.getElementById('style-options')!;
  const overlayOptions = document.getElementById('overlay-options')!;
  const labelOptions = document.getElementById('label-options')!;
  const overlaysSection = document.getElementById('overlays-section')!;

  const toggles = loadToggles();

  const themes = getThemes(hasNauticalSource(), hasAeroSource());

  // Theme options — only show available themes
  themes
    .filter((t) => t.available)
    .forEach((t) => {
      const div = document.createElement('div');
      div.className = 'style-option' + (t.id === currentTheme ? ' active' : '');
      div.textContent = t.label;
      div.addEventListener('click', () => {
        setTheme(map, t.id as ThemeId);
        styleOptions.querySelectorAll('.style-option').forEach((el) => el.classList.remove('active'));
        div.classList.add('active');

        // Show/hide terrain overlays based on theme type
        updateOverlayVisibility();
      });
      styleOptions.appendChild(div);
    });

  // Overlay toggles (terrain — only relevant for vector themes)
  function updateOverlayVisibility() {
    const isRaster = isRasterTheme(currentTheme);
    overlaysSection.style.display = !isRaster && hasTerrainSource() ? '' : 'none';
  }

  if (hasTerrainSource()) {
    overlaysSection.style.display = '';

    addToggle(overlayOptions, 'Hillshade', true, toggles, (on) => {
      setLayerVisibility(map, OVERLAY_GROUPS['Hillshade'] || [], on);
    });

    addToggle(overlayOptions, 'Contour lines', true, toggles, (on) => {
      setLayerVisibility(map, OVERLAY_GROUPS['Contour lines'] || [], on);
    });

    addToggle(overlayOptions, '3D Terrain', false, toggles, () => {
      toggleTerrain(map);
    });

    updateOverlayVisibility();
  }

  // Label toggles
  Object.keys(LABEL_GROUPS).forEach((groupName) => {
    addToggle(labelOptions, groupName, true, toggles, (on) => {
      setLayerVisibility(map, LABEL_GROUPS[groupName], on);
    });
  });

  // Apply saved toggle state when map style loads
  map.on('style.load', () => {
    if (!isRasterTheme(currentTheme)) {
      Object.keys(LABEL_GROUPS).forEach((groupName) => {
        if (toggles[groupName] === false) {
          setLayerVisibility(map, LABEL_GROUPS[groupName], false);
        }
      });
      Object.keys(OVERLAY_GROUPS).forEach((groupName) => {
        if (toggles[groupName] === false) {
          setLayerVisibility(map, OVERLAY_GROUPS[groupName], false);
        }
      });
    }
  });

  // Units toggle — added after the Labels section in the sidebar
  const sidebar = document.getElementById('sidebar')!;
  const unitsSection = document.createElement('div');
  unitsSection.className = 'sidebar-section';

  const unitsHeader = document.createElement('div');
  unitsHeader.className = 'section-header';
  unitsHeader.textContent = 'Units';
  unitsSection.appendChild(unitsHeader);

  const unitsBody = document.createElement('div');
  unitsBody.className = 'section-body units-toggle-group';

  const imperialOpt = document.createElement('div');
  imperialOpt.className = 'style-option' + (getUnits() === 'imperial' ? ' active' : '');
  imperialOpt.textContent = 'Imperial (mi/ft)';

  const metricOpt = document.createElement('div');
  metricOpt.className = 'style-option' + (getUnits() === 'metric' ? ' active' : '');
  metricOpt.textContent = 'Metric (km/m)';

  imperialOpt.addEventListener('click', () => {
    setUnits('imperial');
    imperialOpt.classList.add('active');
    metricOpt.classList.remove('active');
  });
  metricOpt.addEventListener('click', () => {
    setUnits('metric');
    metricOpt.classList.add('active');
    imperialOpt.classList.remove('active');
  });

  unitsBody.appendChild(imperialOpt);
  unitsBody.appendChild(metricOpt);
  unitsSection.appendChild(unitsBody);
  sidebar.appendChild(unitsSection);
}
