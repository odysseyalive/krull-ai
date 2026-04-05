export type Units = 'imperial' | 'metric';

const STORAGE_KEY = 'krull-map-units';

type Listener = (units: Units) => void;
const listeners: Listener[] = [];

let current: Units = (localStorage.getItem(STORAGE_KEY) as Units) || 'imperial';

export function getUnits(): Units {
  return current;
}

export function setUnits(u: Units) {
  current = u;
  localStorage.setItem(STORAGE_KEY, u);
  listeners.forEach((fn) => fn(u));
}

export function onUnitsChange(fn: Listener) {
  listeners.push(fn);
}
