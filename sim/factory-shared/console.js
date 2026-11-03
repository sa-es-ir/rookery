// The overlay, a fixed 1600 x 900 layout in DESIGN.md's tokens: every panel redraws from the frame's state, so nothing
// here can disagree with the machine. All text goes in as textContent.
import {COMMENTERS, GUARDS, HOLD, MAX_STEPS, MAX_TASKS, PHASES, RUN_CAP, SCENARIOS, STATION_NAME, TRIGGER, USD_CAP} from './run.js';

export const VIEWS = ['hero', 'top', 'gate', 'worktree', 'heron', 'review', 'pr', 'follow'];
const VIEW_LABEL = {hero: 'HERO', top: 'PLAN', gate: 'GATE', worktree: 'WORKTREE', heron: 'HERON', review: 'REVIEW', pr: 'PR', follow: 'FOLLOW'};
export const DISPLAY = ['labels', 'wall', 'cut', 'orbit'];
const DISPLAY_LABEL = {labels: 'LABELS', wall: 'NO WALL', cut: 'CUT NEST', orbit: 'ORBIT'};

const CSS = `
#app{font-family:Inter,system-ui,sans-serif;color:#d1cece;-webkit-font-smoothing:antialiased;font-variant-numeric:tabular-nums;font-weight:400}
.ui{position:absolute;inset:0;pointer-events:none}
.mo{font-family:'JetBrains Mono',ui-monospace,monospace}
.cp{position:absolute;background:rgba(26,22,36,.86);border-radius:16px;box-shadow:rgba(255,255,255,.1) 0 0 0 1px inset,rgba(255,142,93,.3) 0 1px 0 0 inset}
.ph{position:absolute;font:400 12px 'JetBrains Mono',monospace;letter-spacing:.8px;color:#9d9797}
.lab{position:absolute;font:400 12px 'JetBrains Mono',monospace;color:#9d9797;white-space:nowrap}
.bt{position:absolute;height:28px;padding:0 8px;display:flex;align-items:center;justify-content:center;border:1px solid rgba(255,255,255,.14);border-radius:8px;background:rgba(13,10,25,.5);font:400 12px 'JetBrains Mono',monospace;color:#d1cece;white-space:nowrap;cursor:pointer;pointer-events:auto}
.bt:hover{border-color:rgba(255,255,255,.34);color:#fff}
.bt:focus-visible{outline:2px solid rgb(107,33,239);outline-offset:2px}
.bt.on{background:#2c2834;border-color:rgba(255,142,93,.6);color:#fff}
.cta{position:absolute;height:40px;padding:0 20px;display:flex;align-items:center;gap:8px;border:0;border-radius:8px;background:linear-gradient(30deg,rgb(253,137,37),rgb(255,12,0));font:400 15px 'JetBrains Mono',monospace;color:#fff;white-space:nowrap;cursor:pointer;pointer-events:auto}
.cta:hover{filter:brightness(1.12)}
.cta:focus-visible{outline:2px solid #fff;outline-offset:2px}
.ph-row{position:absolute;left:44px;width:254px;height:21px;display:flex;align-items:center;gap:8px;font:400 12px 'JetBrains Mono',monospace;color:#6f6a77;white-space:nowrap}
.ph-row i{display:block;width:7px;height:7px;border-radius:9999px;background:#3e3a46;flex:none}
.ph-row.seen{color:#9d9797}
.ph-row.seen i{background:#6f6a77}
.ph-row.now{color:#fff}
.ph-row.now i{background:#ff492c;box-shadow:0 0 8px #ff492c}
.ph-row.now.ok i{background:#3ec98a;box-shadow:0 0 8px #3ec98a}
.ph-row.now.warn i{background:#fd8925;box-shadow:0 0 8px #fd8925}
.task{position:absolute;display:flex;align-items:center;gap:7px;font:400 12px 'JetBrains Mono',monospace;color:#6f6a77;white-space:nowrap}
.task b{display:block;width:12px;height:12px;border-radius:3px;border:1px solid #4b4658;font-weight:400}
.task.run{color:#fff}.task.run b{border-color:#fd8925;box-shadow:0 0 6px #fd8925}
.task.ok{color:#9d9797}.task.ok b{background:#3ec98a;border-color:#3ec98a}
.task.bad{color:#fff}.task.bad b{background:#ff492c;border-color:#ff492c}
.chip{position:absolute;height:22px;padding:0 10px;display:flex;align-items:center;border-radius:9999px;font:400 12px 'JetBrains Mono',monospace;background:#2c2834;color:#fff;white-space:nowrap}
.tag{position:absolute;transform:translate(-50%,-100%);padding:3px 8px;border-radius:9999px;background:rgba(14,9,24,.82);box-shadow:rgba(255,255,255,.14) 0 0 0 1px inset;font:400 12px 'JetBrains Mono',monospace;color:#9d9797;white-space:nowrap;z-index:5}
.tag.now{color:#fff;box-shadow:rgba(255,120,70,.8) 0 0 0 1px inset}
.cur{position:absolute;left:0;top:0;z-index:50;filter:drop-shadow(0 1px 2px rgba(0,0,0,.6));pointer-events:none}
.ring{position:absolute;width:38px;height:38px;margin:-19px 0 0 -19px;border-radius:50%;border:1.5px solid #ff8a5c;opacity:0;z-index:49;pointer-events:none}
.tl{position:absolute;pointer-events:auto;cursor:ew-resize}
.deny{color:#ff8a5c}
`;
const el = (h) => {
  const d = document.createElement('div');
  d.innerHTML = h.trim();
  return d.firstElementChild;
};
const SHORT = (p) => p.replace('rookery:', '');

export function createConsole(app) {
  const style = document.createElement('style');
  style.textContent = CSS;
  document.head.appendChild(style);
  const ui = el(`<div class="ui"></div>`);
  app.appendChild(ui);
  const add = (h) => ui.appendChild(el(h));
  const panel = (x, y, w, h, title) => {
    add(`<div class="cp" style="left:${x}px;top:${y}px;width:${w}px;height:${h}px"></div>`);
    if (title) add(`<div class="ph" style="left:${x + 18}px;top:${y + 8}px">${title}</div>`);
  };
  const btn = {};
  const spots = {};
  const button = (key, label, x, y, w) => {
    const b = el(`<button type="button" class="bt" style="left:${x}px;top:${y}px;width:${w}px"></button>`);
    b.textContent = label;
    ui.appendChild(b);
    btn[key] = b;
    spots[key] = [x + w / 2, y + 14];
    return b;
  };

  // ------------------------------------------------------------ header
  add(`<div style="position:absolute;left:28px;top:18px;font-size:24px;font-weight:300;color:#fff;letter-spacing:-.17px">rookery</div>`);
  add(`<div style="position:absolute;left:28px;top:52px;font-size:14px;color:#9d9797;font-weight:300">The flow, taken apart. From a label to a draft pull request.</div>`);
  add(`<div class="lab" style="right:28px;top:22px">Example run. Time is compressed.</div>`);

  // ------------------------------------------------------------ 01 the labels
  const px = 24;
  const pw = 292;
  panel(px, 86, pw, 262, 'ISSUE LABEL');
  const rows = [TRIGGER, ...PHASES].map((p, i) => {
    const r = el(`<div class="ph-row" style="top:${112 + i * 25}px"><i></i><span></span></div>`);
    r.querySelector('span').textContent = p;
    ui.appendChild(r);
    return r;
  });

  // ------------------------------------------------------------ 02 tasks.md
  panel(px, 360, pw, 208, 'TASKS.MD');
  const tCount = add(`<div class="lab" style="left:${px + 110}px;top:368px;color:#d1cece"></div>`);
  const taskRows = [];
  for (let i = 0; i < MAX_TASKS; i++) {
    const c = i < 8 ? 0 : 1;
    const y = 400 + (c ? i - 8 : i) * 20;
    const r = el(`<div class="task" style="left:${px + 18 + c * 128}px;top:${y}px"><b></b><span></span></div>`);
    r.querySelector('span').textContent = `task ${i + 1}`;
    ui.appendChild(r);
    taskRows.push(r);
  }

  // ------------------------------------------------------------ 03 the caps
  panel(px, 580, pw, 124, 'CAPS, CHECKED BEFORE EVERY CALL');
  add(`<div class="lab" style="left:${px + 18}px;top:610px">claude calls today</div>`);
  const capV = add(`<div class="lab" style="right:${1600 - px - pw + 18}px;top:610px;color:#fff"></div>`);
  add(`<div style="position:absolute;left:${px + 18}px;top:632px;width:${pw - 36}px;height:4px;background:#2c2834;border-radius:2px"></div>`);
  const capF = add(`<div style="position:absolute;left:${px + 18}px;top:632px;height:4px;background:#d1cece;border-radius:2px"></div>`);
  add(`<div class="lab" style="left:${px + 18}px;top:650px">$${USD_CAP} per repo per UTC day</div>`);
  add(`<div class="lab" style="left:${px + 18}px;top:672px">${MAX_TASKS} tasks and ${MAX_STEPS} after steps per issue</div>`);

  // ------------------------------------------------------------ 04 now
  const qx = 1284;
  const qw = 292;
  panel(qx, 86, qw, 192, 'NOW');
  const whoChip = add(`<div class="chip" style="left:${qx + 18}px;top:112px"></div>`);
  const fnV = add(`<div class="lab" style="left:${qx + 18}px;top:142px;color:#9d9797;width:${qw - 36}px;overflow:hidden;text-overflow:ellipsis"></div>`);
  const textV = add(`<div style="position:absolute;left:${qx + 18}px;top:166px;width:${qw - 36}px;font-size:14px;line-height:1.5;font-weight:400;color:#d1cece"></div>`);

  // ------------------------------------------------------------ 05 the run
  panel(qx, 290, qw, 174, 'RUN');
  Object.entries(SCENARIOS).forEach(([k, label], i) => button(k, label, qx + 18 + (i % 2) * 130, 316 + Math.floor(i / 2) * 34, 124));
  add(`<div class="lab" style="left:${qx + 18}px;top:392px">tasks</div>`);
  const nV = add(`<div class="lab" style="left:${qx + 74}px;top:392px;color:#fff"></div>`);
  add(`<div class="lab" style="left:${qx + 18}px;top:428px">after steps</div>`);
  const aV = add(`<div class="lab" style="left:${qx + 110}px;top:428px;color:#fff"></div>`);
  const stepper = (key, y, x) => {
    button(key + '-', '−', x, y, 34);
    button(key + '+', '+', x + 40, y, 34);
  };
  stepper('tasks', 386, qx + 184);
  stepper('after', 422, qx + 184);

  // ------------------------------------------------------------ 06 what the agent tries
  panel(qx, 476, qw, 172, 'MAKE THE AGENT TRY');
  Object.entries(GUARDS).forEach(([k, g], i) => button('g-' + k, g.cmd, qx + 18, 502 + i * 34, qw - 36));

  // ------------------------------------------------------------ 07 the view and display row, under the panels
  panel(qx, 660, qw, 136, 'VIEW');
  VIEWS.forEach((v, i) => button(v, VIEW_LABEL[v], qx + 18 + (i % 4) * 65, 686 + Math.floor(i / 4) * 32, 60));
  DISPLAY.forEach((d, i) => button(d, DISPLAY_LABEL[d], qx + 18 + i * 65, 752, 60));
  for (const k of [...VIEWS, ...DISPLAY]) {
    btn[k].style.fontSize = '11px';
    btn[k].style.padding = '0';
  }

  // ------------------------------------------------------------ the comment box: only while the run waits for someone
  const cBox = add(`<div class="cp" style="left:340px;top:664px;width:920px;height:112px;display:none"></div>`);
  const cWhy = add(`<div style="position:absolute;left:364px;top:678px;font-size:16px;color:#fff;display:none"></div>`);
  const cAs = add(`<div class="lab" style="left:364px;top:722px;display:none">comment as</div>`);
  const cBtns = Object.entries(COMMENTERS).map(([k, c], i) => {
    const b = button('as-' + k, c.label, 450 + i * 124, 716, 118);
    b.style.display = 'none';
    return b;
  });
  const cta = el(`<button type="button" class="cta" style="left:1016px;top:710px;display:none"></button>`);
  ui.appendChild(cta);
  btn.comment = cta;
  spots.comment = [1120, 730];
  const cRes = add(`<div class="lab" style="left:364px;top:752px;display:none;font-size:12px"></div>`);

  // ------------------------------------------------------------ the transport
  panel(340, 790, 920, 86, '');
  const play = el(`<button type="button" class="bt" style="left:360px;top:818px;width:40px;height:32px" aria-label="Play or pause"></button>`);
  ui.appendChild(play);
  btn.play = play;
  spots.play = [380, 834];
  const TL = {x: 420, w: 660, y: 834};
  add(`<div style="position:absolute;left:${TL.x}px;top:${TL.y - 1}px;width:${TL.w}px;height:2px;background:#3e3a46"></div>`);
  const tlFill = add(`<div style="position:absolute;left:${TL.x}px;top:${TL.y - 1}px;height:2px;background:linear-gradient(141deg,rgb(7,122,199),rgb(107,33,239))"></div>`);
  const tlKnob = add(`<div style="position:absolute;top:${TL.y - 6}px;width:12px;height:12px;margin-left:-6px;border-radius:9999px;background:#fff"></div>`);
  const tlMarks = add(`<div style="position:absolute;left:0;top:0"></div>`);
  const tlStation = add(`<div class="lab" style="left:${TL.x}px;top:${TL.y - 26}px"></div>`);
  const tlTime = add(`<div class="lab" style="left:${TL.x}px;top:${TL.y + 14}px"></div>`);
  add(`<div class="lab" style="left:1104px;top:808px">speed</div>`);
  const SP = {x: 1104, w: 130, y: 840};
  add(`<div style="position:absolute;left:${SP.x}px;top:${SP.y - 1}px;width:${SP.w}px;height:2px;background:#3e3a46"></div>`);
  const spKnob = add(`<div style="position:absolute;top:${SP.y - 5}px;width:10px;height:10px;margin-left:-5px;border-radius:9999px;background:#d1cece"></div>`);
  const spV = add(`<div class="lab" style="left:${SP.x + SP.w - 36}px;top:808px;color:#fff;width:36px;text-align:right"></div>`);

  // ------------------------------------------------------------ the station tags, the wall's reply, the pointer
  const tags = {};
  const tag = (k, label) => {
    const t = el(`<div class="tag"></div>`);
    t.textContent = label;
    ui.appendChild(t);
    tags[k] = t;
  };
  Object.entries(STATION_NAME).forEach(([k, v]) => tag(k, v));
  tag('worktree', 'WORKTREE');
  tag('heron', 'THE ORCHESTRATOR');
  tag('pr', 'DRAFT PR');
  tag('merge', 'MERGE: YOURS');
  const deny = add(`<div class="cp" style="left:500px;top:96px;width:600px;height:64px;display:none"></div>`);
  const denyA = add(`<div class="mo deny" style="position:absolute;left:520px;top:106px;font-size:14px;display:none"></div>`);
  const denyB = add(`<div style="position:absolute;left:520px;top:130px;font-size:14px;color:#d1cece;display:none"></div>`);
  const cursor = add(`<svg class="cur" width="24" height="24" viewBox="0 0 24 24"><path d="M3 2l6.8 18 2.7-7.2L20 10z" fill="#fff" stroke="#0e0918" stroke-width="1.4"/></svg>`);
  const ring = add(`<div class="ring"></div>`);

  const show = (nodes, on) => nodes.forEach((n) => (n.style.display = on ? (n.classList.contains('bt') || n.classList.contains('cta') ? 'flex' : 'block') : 'none'));
  let markRun = null;
  let lastHold = '';

  const update = (s) => {
    const st = s.state;
    // labels
    const seen = new Set(st.run.beats.filter((b) => b.t0 <= st.t).map((b) => b.phase));
    [TRIGGER, ...PHASES].forEach((p, i) => {
      const now = p === st.phase;
      rows[i].className = 'ph-row' + (now ? ' now' : seen.has(p) ? ' seen' : '') + (now && p === 'rookery:done' ? ' ok' : now && p === 'rookery:changes-wanted' ? ' warn' : '');
    });
    // tasks.md
    const n = st.run.n;
    tCount.textContent = st.planOn > 0.5 ? `${st.ticks} of ${n} ticked` : 'no plan yet';
    taskRows.forEach((r, i) => {
      const vis = st.planOn > 0.5 && i < n;
      r.style.display = vis ? 'flex' : 'none';
      if (vis) r.className = 'task ' + (i < st.ticks ? 'ok' : i === st.task ? (st.fail ? 'bad' : 'run') : '');
    });
    // caps
    capV.textContent = `${st.calls} of ${RUN_CAP}`;
    capF.style.width = `${(Math.min(1, st.calls / RUN_CAP) * 256).toFixed(1)}px`;
    // now
    whoChip.textContent = st.who === 'you' ? 'YOU' : st.who === 'agent' ? `AGENT · ${st.model}` : 'ROOKERY';
    whoChip.style.background = st.who === 'agent' ? '#2c2834' : st.who === 'you' ? 'rgba(253,137,37,.26)' : 'rgba(47,143,214,.26)';
    fnV.textContent = st.fn;
    textV.textContent = st.text;
    // buttons
    for (const k of Object.keys(SCENARIOS)) btn[k].classList.toggle('on', st.run.scenario === k);
    nV.textContent = `${n} of ${MAX_TASKS}`;
    aV.textContent = `${st.run.steps} of ${MAX_STEPS}`;
    for (const v of VIEWS) btn[v].classList.toggle('on', s.view === v);
    for (const d of DISPLAY) btn[d].classList.toggle('on', !!s.display[d]);
    for (const [k] of Object.entries(COMMENTERS)) btn['as-' + k].classList.toggle('on', s.as === k);
    // comment box
    const h = st.hold || st.holdSoon;
    show([cBox, cWhy, cAs, cRes, ...cBtns, cta], !!h);
    if (h) {
      if (h !== lastHold) {
        cWhy.textContent = HOLD[h].q;
        cta.textContent = `Comment “${HOLD[h].verb}”`;
      }
      const c = s.denied;
      cRes.textContent = c ? `Denied: ${COMMENTERS[c].why}` : 'Only a comment on the issue moves this. The portal has no approve button.';
      cRes.className = 'lab' + (c ? ' deny' : '');
      cta.style.opacity = st.hold ? 1 : 0.55;
    }
    lastHold = h;
    // transport
    const prog = st.t / st.run.dur;
    tlFill.style.width = `${prog * TL.w}px`;
    tlKnob.style.left = `${TL.x + prog * TL.w}px`;
    if (markRun !== st.run) {
      markRun = st.run;
      tlMarks.replaceChildren(...st.run.holds.map((m) => el(`<div style="position:absolute;left:${TL.x + (m.t / st.run.dur) * TL.w - 3}px;top:${TL.y - 8}px;width:6px;height:16px;border-radius:2px;background:#ff492c"></div>`)));
    }
    tlStation.textContent = STATION_NAME[st.station] || '';
    tlTime.textContent = st.hold ? 'waiting for a comment' : s.paused ? 'paused' : `${st.t.toFixed(1)} s of ${st.run.dur.toFixed(1)} s`;
    play.textContent = s.paused ? '▶' : '❚❚';
    spKnob.style.left = `${SP.x + s.speedK * SP.w}px`;
    spV.textContent = `${s.speed.toFixed(1)}×`;
    // tags
    const lit = {...tags};
    for (const k of Object.keys(lit)) {
      const a = s.tags?.[k];
      const t = tags[k];
      const inView = a && a[0] > 330 && a[0] < 1270 && a[1] > 92 && a[1] < 660;
      if (!inView || !s.display.labels) {
        t.style.display = 'none';
        continue;
      }
      t.style.display = 'block';
      t.style.left = `${a[0].toFixed(1)}px`;
      t.style.top = `${a[1].toFixed(1)}px`;
      t.classList.toggle('now', k === st.station || (k === 'worktree' && st.working) || (k === 'heron' && (st.tickBeat || st.key === 'commit')) || (k === 'pr' && st.prUp > 0.05));
    }
    // what the wall says
    const g = s.guard;
    show([deny, denyA, denyB], !!g);
    if (g) {
      denyA.textContent = `$ ${GUARDS[g.kind].cmd}   →   ${GUARDS[g.kind].rule}`;
      denyB.textContent = GUARDS[g.kind].why;
    }
    // the pointer
    if (s.cursor && s.cursor.x > -50) {
      cursor.style.display = 'block';
      cursor.style.transform = `translate(${s.cursor.x}px,${s.cursor.y}px) scale(${s.cursor.press ? 0.86 : 1})`;
    } else cursor.style.display = 'none';
    if (s.cursor && s.cursor.ring > 0) {
      ring.style.left = `${s.cursor.ringX}px`;
      ring.style.top = `${s.cursor.ringY}px`;
      ring.style.opacity = s.cursor.ring;
      ring.style.transform = `scale(${1.5 - s.cursor.ring * 0.7})`;
    } else ring.style.opacity = 0;
  };
  return {root: ui, update, btn, spots, TL, SP};
}
export {SHORT};
