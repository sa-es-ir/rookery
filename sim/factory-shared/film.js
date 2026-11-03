// The tour: a SCRIPT of operator moves and render(t), a pure function of time. The live page plays the same script
// when nobody touches it, and capture.mjs records it. The runs and every tau are tabulated once at load.
import {runSim} from 'teardown-kit/runtime.js';
import * as THREE from 'three';
import {createConsole, DISPLAY} from './console.js';
import {buildRun, stateAt, COMMENTERS} from './run.js';
import {createStage} from './stage.js';

export const SCRIPT = [
  {t: 0, run: {scenario: 'clean', tasks: 3, after: 0}, view: 'hero', as: 'writer'},
  {t: 3.4, view: 'follow'},
  {t: 10.4, as: 'reader'},
  {t: 11, press: 'reader'},
  {t: 13, as: 'bot'},
  {t: 13.6, press: 'bot'},
  {t: 15.6, as: 'writer'},
  {t: 16.2, press: 'writer'},
  {t: 18.2, view: 'worktree'},
  {t: 22.7, guard: 'push'},
  {t: 25.4, view: 'heron'},
  {t: 29.4, view: 'follow'},
  {t: 37.5, view: 'pr'},
  {t: 41.8, run: {scenario: 'failure', tasks: 2, after: 0}, view: 'hero', as: 'writer', rate: 2.2},
  {t: 46.8, press: 'writer'},
  {t: 47.4, view: 'worktree'},
  {t: 50.4, press: 'writer'},
  {t: 53.6, view: 'hero'},
];
export const DURATION = 60.5;
export const DEFAULTS = {duration: DURATION, fps: 60, theme: 'dark', hud: {enabled: false, title: 'rookery', subtitle: 'the flow, taken apart'}, director: {enabled: false}, watermark: ''};

const D2R = Math.PI / 180;
const smoother = (x) => {
  const u = Math.min(1, Math.max(0, x));
  return u * u * u * (u * (u * 6 - 15) + 10);
};
const ss = (a, b, t) => smoother((t - a) / (b - a));

// tau at film time t, walking the script through the holds; a press by a writer lifts the hold it reaches
const DT = 1 / 120;
const RUNS = [];
const TAB = [];
{
  let cur = null;
  let tau = 0;
  let rate = 1;
  const free = new Set();
  let j = 0;
  for (let i = 0; i * DT <= DURATION + 1; i++) {
    const t = i * DT;
    while (j < SCRIPT.length && SCRIPT[j].t <= t) {
      const e = SCRIPT[j++];
      if (e.run) {
        cur = buildRun(e.run);
        RUNS.push(cur);
        tau = 0;
        free.clear();
      }
      if (e.rate) rate = e.rate;
      if (e.press && COMMENTERS[e.press].ok) {
        const h = cur.holds.find((x) => x.t >= tau - 1e-6 && !free.has(x.t));
        if (h) free.add(h.t);
      }
    }
    const next = cur.holds.find((h) => h.t >= tau - 1e-6 && !free.has(h.t));
    tau += DT * rate;
    if (next && tau >= next.t) tau = next.t;
    if (tau > cur.dur) tau = cur.dur;
    TAB.push(tau, RUNS.length - 1);
  }
}
export const runAt = (t) => {
  const i = Math.max(0, Math.min(TAB.length / 2 - 2, Math.floor(t / DT)));
  const run = RUNS[TAB[i * 2 + 1]];
  const same = TAB[(i + 1) * 2 + 1] === TAB[i * 2 + 1];
  const u = t / DT - i;
  const tau = same ? TAB[i * 2] + (TAB[(i + 1) * 2] - TAB[i * 2]) * Math.max(0, Math.min(1, u)) : TAB[i * 2];
  return {run, tau};
};

// the display toggles at film time t, and the last press and guard
export const displayAt = (t) => {
  const out = Object.fromEntries(DISPLAY.map((k) => [k, 0]));
  out.labels = 1;
  for (const e of SCRIPT) if (e.display && e.t <= t) out[e.display] = out[e.display] ? 0 : 1;
  return out;
};
export const asAt = (t) => SCRIPT.filter((e) => e.as && e.t <= t).pop()?.as || 'writer';
export const deniedAt = (t) => {
  const e = SCRIPT.filter((x) => x.press && x.t <= t).pop();
  return e && !COMMENTERS[e.press].ok && t - e.t < 1.9 ? {who: e.press, age: t - e.t} : null;
};
export const guardAt = (t) => {
  const e = SCRIPT.filter((x) => x.guard && x.t <= t).pop();
  return e && t - e.t < 3.2 ? {kind: e.guard, age: t - e.t} : null;
};

// ------------------------------------------------------------ views, u = seconds on it
const STATION_VIEW = {intake: 'intake', plan: 'plan', gate: 'gate', dock: 'heron', after: 'after', review: 'review', push: 'pr'};
export const VIEW_FN = {
  hero: (u) => ({tx: 0, ty: 0.3, tz: 0.6, az: -8 + 1.1 * u, el: 34, d: 30.5, sx: -4, sy: -50}),
  top: (u) => ({tx: 0, ty: 0, tz: 0.8, az: 0.6 * u, el: 76, d: 31, sx: -4, sy: -50}),
  // the close views stand outside the nest and look in over the rim, so no other machine is in the way
  intake: (u) => ({tx: -3, ty: 1.3, tz: 3.6, az: -40 + 1.2 * u, el: 25, d: 19, sx: 0, sy: -60}),
  plan: (u) => ({tx: -4.7, ty: 1.2, tz: -0.4, az: -88 + 1.2 * u, el: 26, d: 19, sx: 0, sy: -60}),
  gate: (u) => ({tx: -2.35, ty: 1.5, tz: -4.07, az: -78 + 1 * u, el: 24, d: 20, sx: 0, sy: -90}),
  worktree: (u) => ({tx: 0, ty: 0.9, tz: 0, az: 4 + 1.4 * u, el: 32, d: 17, sx: 0, sy: -40}),
  heron: (u) => ({tx: 0.9, ty: 1.3, tz: -2.2, az: 28 + 1.3 * u, el: 38, d: 18, sx: 0, sy: -40}),
  after: (u) => ({tx: 4.26, ty: 1.1, tz: -1.99, az: 62 + 1 * u, el: 27, d: 18, sx: 0, sy: -60}),
  review: (u) => ({tx: 4.54, ty: 1.2, tz: 1.22, az: -12 + 1 * u, el: 28, d: 18, sx: 0, sy: -60}),
  pr: (u) => ({tx: 3.9, ty: 1.2, tz: 5.3, az: 30 + 1 * u, el: 21, d: 22, sx: 0, sy: -40}),
};
VIEW_FN.follow = (u, st) => VIEW_FN[STATION_VIEW[st?.station] || 'hero'](u);

const VIEWS = SCRIPT.filter((e) => e.view).map((e) => [e.t, e.view]);
export const viewAt = (t) => VIEWS.filter(([a]) => a <= t).pop();
const blend = (a, b, k) => {
  const o = {};
  for (const key in b) o[key] = key === 'az' ? a.az + ((((b.az - a.az + 540) % 360) - 180) * k) : a[key] + (b[key] - a[key]) * k;
  return o;
};
// follow changes target whenever the station changes, so it is eased from the state it is leaving
export const camFor = (view, u, st) => {
  if (view !== 'follow' || !st.moving) return VIEW_FN[view](u, st);
  const a = VIEW_FN[STATION_VIEW[st.beat.from]](u);
  const b = VIEW_FN[STATION_VIEW[st.beat.to]](u);
  return blend(a, b, smoother(st.k));
};
export const camAt = (t, st) => {
  let i = 0;
  while (i < VIEWS.length - 1 && VIEWS[i + 1][0] <= t) i++;
  const cur = camFor(VIEWS[i][1], t - VIEWS[i][0], st);
  if (i === 0) return cur;
  const k = ss(VIEWS[i][0], VIEWS[i][0] + 1.5, t);
  if (k >= 1) return cur;
  const prev = VIEWS[i - 1][1];
  return blend(camFor(prev, t - VIEWS[i - 1][0], st), cur, k);
};

// ------------------------------------------------------------ the operator's pointer
const REST = [[800, 420], [720, 500], [880, 380], [760, 440]];
export function makeCursor(ui) {
  const EV = SCRIPT.filter((e) => e.t > 0).flatMap((e) => {
    const out = [];
    if (e.as) out.push({t: e.t, spot: ui.spots['as-' + e.as]});
    if (e.press) out.push({t: e.t, spot: ui.spots.comment});
    if (e.view) out.push({t: e.t, spot: ui.spots[e.view]});
    if (e.guard) out.push({t: e.t, spot: ui.spots['g-' + e.guard]});
    if (e.run) out.push({t: e.t, spot: ui.spots[e.run.scenario]});
    return out;
  });
  const WP = [[0, 900, 560]];
  EV.forEach((e, i) => {
    WP.push([e.t - 0.2, ...e.spot], [e.t + 0.22, ...e.spot]);
    WP.push([e.t + 1.3, ...REST[i % REST.length]]);
  });
  WP.sort((a, b) => a[0] - b[0]);
  return (t) => {
    let i = 0;
    while (i < WP.length - 2 && t > WP[i + 1][0]) i++;
    const [t0, x0, y0] = WP[i];
    const [t1, x1, y1] = WP[i + 1];
    const k = ss(t0, t1, t);
    let press = false;
    let ring = 0;
    let rx = 0;
    let ry = 0;
    for (const e of EV) {
      if (t >= e.t - 0.07 && t < e.t + 0.09) press = true;
      if (t >= e.t && t < e.t + 0.55) {
        ring = 1 - (t - e.t) / 0.55;
        [rx, ry] = e.spot;
      }
    }
    return {x: x0 + (x1 - x0) * k, y: y0 + (y1 - y0) * k, press, ring, ringX: rx, ringY: ry};
  };
}

// ------------------------------------------------------------ where the tags sit, in world space
export function makeTags(st) {
  const f = st.factory;
  const v = new THREE.Vector3();
  const place = (group, a) => st.project(v.copy(a).applyMatrix4(group.matrixWorld));
  return () => {
    const out = {};
    const g = f.stationRot;
    for (const k of ['intake', 'plan', 'gate', 'after', 'review', 'push']) out[k] = place(g[k], f.anchors[k]);
    out.dock = null; // the heron's tag covers this spot
    out.worktree = st.project(v.copy(f.anchors.worktree));
    out.heron = st.project(v.copy(f.anchors.heron));
    out.pr = place(f.pr, f.anchors.pr);
    out.merge = place(f.door, f.anchors.merge);
    return out;
  };
}

export function makeFilm() {
  return async ({canvas, capture}) => {
    const st = createStage({canvas});
    const resize = () => st.resize(1600, 900, capture ? devicePixelRatio || 1 : Math.min(devicePixelRatio || 1, 2));
    resize();
    const ui = createConsole(document.getElementById('app'));
    const cursorAt = makeCursor(ui);
    const tags = makeTags(st);
    return {
      resize,
      render(t) {
        const {run, tau} = runAt(t);
        const s = stateAt(run, tau);
        const disp = displayAt(t);
        const cam = camAt(t, s);
        const guard = guardAt(t);
        const denied = deniedAt(t);
        st.frame(s, {T: t, guard, deny: denied ? Math.exp(-denied.age * 2.2) : 0, noWall: 0, cut: 0}, {...cam, az: cam.az * D2R, el: cam.el * D2R});
        ui.update({state: s, view: viewAt(t)[1], display: disp, as: asAt(t), denied: denied?.who, guard, tags: tags(), cursor: cursorAt(t), paused: false, speed: 1, speedK: 0.47});
        return {};
      },
    };
  };
}

export const run = () => runSim(DEFAULTS, makeFilm());
