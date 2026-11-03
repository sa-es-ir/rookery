// The factory as a page you drive. Left alone it plays film.js's script with a pointer, looped. A click, drag, wheel or
// key hands control to the visitor, and 25 s of quiet hands it back. A run stops at its holds until a comment from
// someone with write access lifts them, as on GitHub.
import {warmComposer} from 'teardown-kit/warm.js';
import {createConsole, DISPLAY, VIEWS} from './console.js';
import {asAt, camAt, camFor, deniedAt, displayAt, DURATION, guardAt, makeCursor, makeTags, runAt, viewAt, VIEW_FN} from './film.js';
import {buildRun, COMMENTERS, GUARDS, MAX_STEPS, MAX_TASKS, SCENARIOS, stateAt} from './run.js';
import {createStage} from './stage.js';

const W = 1600;
const H = 900;
const IDLE = 25;
const D2R = Math.PI / 180;
const clamp = (x, a, b) => Math.min(b, Math.max(a, x));
const wrap = (d) => ((((d + 180) % 360) + 360) % 360) - 180;
const SPEED = [0.25, 4];
const speedK = (v) => (v - SPEED[0]) / (SPEED[1] - SPEED[0]);

const CSS = `
html,body{background:#0e0918;overflow:hidden;margin:0}
#app{position:absolute!important;inset:auto!important;left:0;top:0;width:${W}px;height:${H}px;transform-origin:0 0}
#gl{touch-action:none;cursor:grab;position:absolute;left:0;top:0;width:${W}px;height:${H}px}
#gl.drag{cursor:grabbing}
.hit{position:absolute;pointer-events:auto;cursor:ew-resize}
.tourchip{position:absolute;left:800px;top:22px;transform:translateX(-50%);display:flex;align-items:center;gap:8px;height:30px;padding:0 14px;border:1px solid rgba(255,255,255,.14);border-radius:8px;background:rgba(13,10,25,.6);color:#d1cece;font:400 12px 'JetBrains Mono',monospace;white-space:nowrap;cursor:pointer;pointer-events:auto}
.tourchip:hover{color:#fff;border-color:rgba(255,255,255,.34)}
.tourchip i{display:block;width:7px;height:7px;border-radius:50%;background:#ff492c}
.tourchip.on i{animation:blink 1.2s steps(2) infinite}
@keyframes blink{50%{opacity:.15}}
.turn{position:fixed;left:0;right:0;bottom:18px;text-align:center;font:400 12px/1.4 'JetBrains Mono',monospace;color:#9d9797;display:none}
@media (orientation:portrait) and (max-width:820px){.turn{display:block}}
`;

export function runLive() {
  document.title = 'Rookery, the flow taken apart';
  const style = document.createElement('style');
  style.textContent = CSS;
  document.head.appendChild(style);
  const app = document.getElementById('app');
  const canvas = document.createElement('canvas');
  canvas.id = 'gl';
  app.appendChild(canvas);
  const st = createStage({canvas});
  const ui = createConsole(app);
  const tourCursor = makeCursor(ui);
  const tagsAt = makeTags(st);

  // ------------------------------------------------------------ quality tiers
  const TIERS = [
    {samples: 4, bloom: true, shadow: 2048, pr: 1.75},
    {samples: 2, bloom: true, shadow: 1024, pr: 1.25},
    {samples: 0, bloom: false, shadow: 512, pr: 1},
  ];
  const light = matchMedia('(pointer: coarse)').matches || Math.min(innerWidth, innerHeight) < 700;
  let tier = light ? 2 : 0;
  let scale = 1;
  const fit = () => {
    scale = Math.min(innerWidth / W, innerHeight / H);
    app.style.transform = `translate(${(innerWidth - W * scale) / 2}px, ${(innerHeight - H * scale) / 2}px) scale(${scale})`;
    st.resize(W, H, clamp((devicePixelRatio || 1) * scale, 0.75, TIERS[tier].pr));
  };
  const applyTier = () => {
    const {pr, ...q} = TIERS[tier];
    st.setQuality(q);
    fit();
  };
  applyTier();
  addEventListener('resize', fit);
  const toStage = (cx, cy) => {
    const r = app.getBoundingClientRect();
    return [(cx - r.left) / scale, (cy - r.top) / scale];
  };
  const tourChip = document.createElement('button');
  tourChip.type = 'button';
  tourChip.className = 'tourchip';
  ui.root.appendChild(tourChip);
  const note = document.createElement('div');
  note.className = 'turn';
  note.textContent = 'Turn your phone sideways to see the full page';
  document.body.appendChild(note);

  // ------------------------------------------------------------ state
  let T = 0;
  let cfg = {scenario: 'clean', tasks: 4, after: 0};
  let run = buildRun(cfg);
  let tau = 0;
  let free = new Set();
  let speed = 1;
  let paused = false;
  let view = 'hero';
  let viewAt0 = 0;
  let as = 'writer';
  let denied = null;
  let guard = null;
  const disp = Object.fromEntries(DISPLAY.map((k) => [k, 0]));
  disp.labels = 1;
  const off = {az: 0, el: 0, k: 1};
  const s0 = stateAt(run, 0);
  const cam = {...VIEW_FN.hero(0, s0)};
  let ring = {t: -9, x: 0, y: 0};
  const pulse = (k) => {
    const sp = ui.spots[k];
    if (sp) ring = {t: T, x: sp[0], y: sp[1]};
  };

  // ------------------------------------------------------------ the tour
  const AP = {on: !matchMedia('(prefers-reduced-motion: reduce)').matches, t0: 0};
  let lastInput = 0;
  const startTour = () => {
    AP.on = true;
    AP.t0 = T;
    paused = false;
    speed = 1;
    Object.assign(off, {az: 0, el: 0, k: 1});
  };
  const takeOver = () => {
    lastInput = T;
    if (!AP.on) return;
    AP.on = false;
    // keep what the tour was showing: its run, its tau and the holds it already passed
    const c = (T - AP.t0) % DURATION;
    const r = runAt(c);
    run = r.run;
    tau = r.tau;
    cfg = {scenario: run.scenario, tasks: run.n, after: run.steps};
    free = new Set(run.holds.filter((h) => h.t < tau - 1e-6).map((h) => h.t));
    const [t0, v] = viewAt(c);
    view = v;
    viewAt0 = T - (c - t0);
    as = asAt(c);
    Object.assign(disp, displayAt(c));
  };
  tourChip.addEventListener('click', () => {
    lastInput = T;
    if (AP.on) takeOver();
    else startTour();
  });
  addEventListener('pointerdown', (e) => {
    if (e.target !== tourChip) takeOver();
  });

  // ------------------------------------------------------------ controls
  const restart = () => {
    run = buildRun(cfg);
    tau = 0;
    free = new Set();
    denied = null;
    paused = false;
  };
  for (const k of Object.keys(SCENARIOS))
    ui.btn[k].addEventListener('click', () => {
      takeOver();
      pulse(k);
      cfg = {...cfg, scenario: k};
      restart();
    });
  const stepper = (key, field, max, min) => {
    ui.btn[key + '-'].addEventListener('click', () => {
      takeOver();
      pulse(key + '-');
      cfg = {...cfg, [field]: clamp(cfg[field] - 1, min, max)};
      restart();
    });
    ui.btn[key + '+'].addEventListener('click', () => {
      takeOver();
      pulse(key + '+');
      cfg = {...cfg, [field]: clamp(cfg[field] + 1, min, max)};
      restart();
    });
  };
  stepper('tasks', 'tasks', MAX_TASKS, 1);
  stepper('after', 'after', MAX_STEPS, 0);
  for (const v of VIEWS)
    ui.btn[v].addEventListener('click', () => {
      takeOver();
      pulse(v);
      view = v;
      viewAt0 = T;
      Object.assign(off, {az: 0, el: 0, k: 1});
    });
  for (const k of DISPLAY)
    ui.btn[k].addEventListener('click', () => {
      takeOver();
      pulse(k);
      disp[k] = disp[k] ? 0 : 1;
      if (k === 'orbit') off.orbit0 = T;
    });
  for (const k of Object.keys(COMMENTERS))
    ui.btn['as-' + k].addEventListener('click', () => {
      takeOver();
      pulse('as-' + k);
      as = k;
      denied = null;
    });
  for (const k of Object.keys(GUARDS))
    ui.btn['g-' + k].addEventListener('click', () => {
      takeOver();
      pulse('g-' + k);
      guard = {kind: k, t0: T};
    });
  ui.btn.comment.addEventListener('click', () => {
    takeOver();
    pulse('comment');
    const h = run.holds.find((x) => x.t >= tau - 1e-6 && !free.has(x.t));
    if (!h) return;
    if (COMMENTERS[as].ok) {
      free.add(h.t);
      denied = null;
      paused = false;
    } else denied = {who: as, t0: T};
  });
  ui.btn.play.addEventListener('click', () => {
    takeOver();
    pulse('play');
    if (tau >= run.dur - 1e-3) restart();
    else paused = !paused;
  });
  const slider = (x0, w, y, apply) => {
    const d = document.createElement('div');
    d.className = 'hit';
    Object.assign(d.style, {left: `${x0 - 8}px`, top: `${y - 12}px`, width: `${w + 16}px`, height: '24px'});
    const move = (e) => {
      const [x] = toStage(e.clientX, e.clientY);
      apply(clamp((x - x0) / w, 0, 1));
      lastInput = T;
    };
    d.addEventListener('pointerdown', (e) => {
      takeOver();
      d.setPointerCapture(e.pointerId);
      const up = () => {
        removeEventListener('pointermove', move);
        removeEventListener('pointerup', up);
      };
      addEventListener('pointermove', move);
      addEventListener('pointerup', up);
      move(e);
    });
    ui.root.appendChild(d);
  };
  // scrubbing past a hold counts as lifting it, and scrubbing back before one shuts it again
  slider(ui.TL.x, ui.TL.w, ui.TL.y, (k) => {
    tau = k * run.dur;
    free = new Set(run.holds.filter((h) => h.t < tau - 1e-6).map((h) => h.t));
    paused = true;
    denied = null;
  });
  slider(ui.SP.x, ui.SP.w, ui.SP.y, (k) => (speed = SPEED[0] + k * (SPEED[1] - SPEED[0])));
  addEventListener('keydown', (e) => {
    if (e.altKey || e.ctrlKey || e.metaKey) return;
    if (e.target?.tagName === 'BUTTON' && (e.key === ' ' || e.key === 'Enter')) {
      takeOver();
      return; // the focused button takes it
    }
    const key = e.key.toLowerCase();
    const v = {1: 'hero', 2: 'top', 3: 'gate', 4: 'worktree', 5: 'heron', 6: 'review', 7: 'pr', 8: 'follow'}[key];
    if (v) ui.btn[v].click();
    else if (key === ' ') ui.btn.play.click();
    else if (key === 'a') ui.btn.comment.click();
    else return;
    e.preventDefault();
  });
  let drag = null;
  canvas.addEventListener('pointerdown', (e) => {
    drag = {x: e.clientX, y: e.clientY};
    canvas.setPointerCapture(e.pointerId);
    canvas.classList.add('drag');
  });
  canvas.addEventListener('pointermove', (e) => {
    if (!drag) return;
    off.az += ((e.clientX - drag.x) / scale) * 0.3;
    off.el = clamp(off.el + ((e.clientY - drag.y) / scale) * 0.2, -25, 55);
    drag = {x: e.clientX, y: e.clientY};
    lastInput = T;
  });
  const endDrag = () => {
    drag = null;
    canvas.classList.remove('drag');
  };
  canvas.addEventListener('pointerup', endDrag);
  canvas.addEventListener('pointercancel', endDrag);
  canvas.addEventListener(
    'wheel',
    (e) => {
      e.preventDefault();
      takeOver();
      off.k = clamp(off.k * Math.exp(e.deltaY * 0.0012), 0.35, 1.8);
    },
    {passive: false},
  );

  // ------------------------------------------------------------ the frame
  let last = performance.now();
  const perf = {n: 0, sum: 0, slow: 0, age: 0};
  const frame = (now) => {
    const dt = Math.min(0.1, Math.max(0, (now - last) / 1000));
    last = now;
    T += dt;
    if (tier < TIERS.length - 1) {
      perf.age += dt;
      if (perf.age > 0.6) {
        perf.sum += dt;
        perf.n++;
        if (perf.sum >= 0.5 && perf.n >= 6) {
          perf.slow = perf.sum / perf.n > 0.022 ? perf.slow + 1 : 0;
          perf.sum = 0;
          perf.n = 0;
          if (perf.slow >= (perf.age < 6 ? 1 : 2)) {
            tier++;
            applyTier();
            Object.assign(perf, {age: 0, slow: 0});
          }
        }
      }
    }

    let s;
    let tgt;
    let cursor;
    let shownView;
    let shownDisp = disp;
    let shownAs = as;
    let shownDenied;
    let g;
    let deny = 0;
    if (AP.on) {
      const c = (T - AP.t0) % DURATION;
      const r = runAt(c);
      s = stateAt(r.run, r.tau);
      tgt = camAt(c, s);
      shownView = viewAt(c)[1];
      shownDisp = displayAt(c);
      shownAs = asAt(c);
      const d = deniedAt(c);
      shownDenied = d?.who;
      deny = d ? Math.exp(-d.age * 2.2) : 0;
      g = guardAt(c);
      cursor = tourCursor(c);
    } else {
      if (T - lastInput > IDLE && !matchMedia('(prefers-reduced-motion: reduce)').matches) startTour();
      if (!paused) {
        const next = run.holds.find((h) => h.t >= tau - 1e-6 && !free.has(h.t));
        tau += dt * speed;
        if (next && tau >= next.t) tau = next.t;
        tau = Math.min(run.dur, tau);
      }
      s = stateAt(run, tau);
      tgt = {...camFor(view, T - viewAt0, s)};
      tgt.az += off.az + (disp.orbit ? 12 * (T - (off.orbit0 ?? T)) : 0);
      tgt.el = clamp(tgt.el + off.el, 6, 82);
      tgt.d *= off.k;
      shownView = view;
      if (denied && T - denied.t0 < 2.6) {
        shownDenied = denied.who;
        deny = Math.exp(-(T - denied.t0) * 2.2);
      }
      if (guard && T - guard.t0 < 3.2) g = {kind: guard.kind, age: T - guard.t0};
      const rk = Math.max(0, 1 - (T - ring.t) / 0.55);
      cursor = {x: -99, y: -99, press: false, ring: rk, ringX: ring.x, ringY: ring.y};
    }

    const k = 1 - Math.exp(-dt / (AP.on ? 0.14 : 0.34));
    cam.az += wrap(tgt.az - cam.az) * k;
    for (const key of ['tx', 'ty', 'tz', 'el', 'd', 'sx', 'sy']) cam[key] += (tgt[key] - cam[key]) * k;
    st.frame(s, {T, guard: g, deny, noWall: shownDisp.wall ? 1 : 0, cut: shownDisp.cut ? 1 : 0}, {...cam, az: cam.az * D2R, el: cam.el * D2R});
    ui.update({state: s, view: shownView, display: shownDisp, as: shownAs, denied: shownDenied, guard: g, tags: tagsAt(), cursor, paused: AP.on ? false : paused, speed: AP.on ? 1 : speed, speedK: speedK(AP.on ? 1 : speed)});
    if (tourChip.classList.contains('on') !== AP.on || !tourChip.firstChild) {
      tourChip.classList.toggle('on', AP.on);
      tourChip.innerHTML = AP.on ? '<i></i>Auto tour. Click to take over.' : '▶ Resume tour';
    }
    if (!window.SIM_SHOWN) {
      window.SIM_SHOWN = true;
      dispatchEvent(new Event('simframe'));
    }
    requestAnimationFrame(frame);
  };
  // every shader compiles at once, off the main thread, before the first frame
  warmComposer(st.renderer, st.composer, st.scene, st.camera).then(() => requestAnimationFrame(frame));
  window.LIVE = {st, startTour, takeOver, get tier() {
    return tier;
  }, get state() {
    return {tau, run: run.scenario, free: [...free], as, auto: AP.on, view};
  }};
}
