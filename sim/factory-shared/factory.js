// The geometry, all from code: a woven nest (the shape of the logo's), a belt around its inside, one machine per
// station, a glass worktree with one egg per task, and the heron: the orchestrator, the only thing that ticks and
// commits. update(state, guard, T) poses all of it from run.js's state, with no state of its own.
import * as THREE from 'three';
import {mergeGeometries} from 'three/addons/utils/BufferGeometryUtils.js';
import {rng} from 'teardown-kit/util.js';
import {AT, MAX_STEPS, MAX_TASKS} from './run.js';

const V3 = THREE.Vector3;
const D2R = Math.PI / 180;
export const R = 4.7; // the belt's radius
export const WALL = 2.45; // the worktree's radius
const COL = {ash: '#d1cece', steel: '#4b4658', dark: '#2c2834', panel: '#1b1728', scorch: '#ff492c', amber: '#fd8925', blue: '#2f8fd6', violet: '#6b21ef', green: '#3ec98a'};
export const ringPos = (deg, r = R, y = 0) => new V3(r * Math.sin(deg * D2R), y, r * Math.cos(deg * D2R));

const std = (color, o = {}) => new THREE.MeshStandardMaterial({color, roughness: 0.62, metalness: 0.15, envMapIntensity: 0.6, ...o});
const lampMat = (color) => std('#15121d', {emissive: new THREE.Color(color), emissiveIntensity: 0, roughness: 0.4});
const mesh = (geo, mat, x = 0, y = 0, z = 0, cast = true) => {
  const m = new THREE.Mesh(geo, mat);
  m.position.set(x, y, z);
  m.castShadow = cast;
  m.receiveShadow = true;
  return m;
};
const box = (w, h, d, mat, x, y, z) => mesh(new THREE.BoxGeometry(w, h, d), mat, x, y, z);
const cyl = (r, h, mat, x, y, z, seg = 20) => mesh(new THREE.CylinderGeometry(r, r, h, seg), mat, x, y, z);
const smooth = (x) => {
  const u = Math.min(1, Math.max(0, x));
  return u * u * (3 - 2 * u);
};

// ------------------------------------------------------------ the nest: twigs wound around a bowl
function buildNest(clip) {
  const r = rng(746337);
  const geos = [];
  const c = new THREE.Color();
  const twig = (r0, y0, span, thick, tone, wob) => {
    const a0 = r() * 360;
    const pts = [];
    const n = 7;
    for (let i = 0; i <= n; i++) {
      const u = i / n;
      const a = a0 + span * u;
      const rr = r0 + (r() - 0.5) * wob + Math.sin(u * Math.PI) * 0.15;
      pts.push(ringPos(a, rr, Math.max(0.02, y0 + (r() - 0.5) * wob * 0.7)));
    }
    const g = new THREE.TubeGeometry(new THREE.CatmullRomCurve3(pts), 22, thick, 5);
    c.set(tone);
    const k = 0.75 + r() * 0.5;
    const col = new Float32Array(g.attributes.position.count * 3);
    for (let i = 0; i < col.length; i += 3) {
      col[i] = c.r * k;
      col[i + 1] = c.g * k;
      col[i + 2] = c.b * k;
    }
    g.setAttribute('color', new THREE.BufferAttribute(col, 3));
    geos.push(g);
  };
  const tones = ['#4a4356', '#5a5266', '#3a3446', '#6a6274'];
  for (let i = 0; i < 150; i++) {
    const h = r();
    // the bowl leans outward as it rises
    twig(6 + h * 1.05 + (r() - 0.5) * 0.25, 0.1 + h * 1.25, 45 + r() * 85, 0.035 + r() * 0.05, tones[Math.floor(r() * 4)], 0.3);
  }
  // a few loose ones crossing the rim, the nest's untidy edge
  for (let i = 0; i < 26; i++) twig(7.1 + r() * 0.5, 1.2 + r() * 0.4, 18 + r() * 30, 0.03, tones[3], 0.8);
  const mat = new THREE.MeshStandardMaterial({vertexColors: true, roughness: 0.78, metalness: 0.05, envMapIntensity: 0.55, clippingPlanes: [clip], clipShadows: true});
  const m = new THREE.Mesh(mergeGeometries(geos), mat);
  m.castShadow = true;
  m.receiveShadow = true;
  for (const g of geos) g.dispose();
  return m;
}

// ------------------------------------------------------------ the belt: chevrons run when the issue moves
const BELT_VERT = /* glsl */ `varying vec2 vP; void main(){ vP = position.xy; gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }`;
const BELT_FRAG = /* glsl */ `uniform float uRun; uniform float uFlow; uniform float uR; varying vec2 vP;
  void main(){
    float a = atan(vP.x, vP.y);
    float r = length(vP) - uR;
    float c = fract(a * 9.0 + uRun + abs(r) * 1.6);
    float chev = (1.0 - smoothstep(0.08, 0.14, abs(c - 0.5)));
    vec3 base = vec3(0.022, 0.017, 0.036);
    vec3 cur = mix(vec3(0.03, 0.48, 0.78), vec3(0.42, 0.13, 0.94), clamp(r + 0.5, 0.0, 1.0));
    gl_FragColor = vec4(base + cur * chev * (0.035 + 0.8 * uFlow), 1.0);
  }`;

function buildBelt() {
  const g = new THREE.Group();
  const a0 = (AT.intake + 20) * D2R;
  const a1 = (AT.push - 18) * D2R;
  const shape = new THREE.RingGeometry(R - 0.52, R + 0.52, 160, 1, Math.PI / 2 - a0, a0 - a1);
  const mat = new THREE.ShaderMaterial({vertexShader: BELT_VERT, fragmentShader: BELT_FRAG, uniforms: {uRun: {value: 0}, uFlow: {value: 0}, uR: {value: R}}, toneMapped: false, polygonOffset: true, polygonOffsetFactor: -2, polygonOffsetUnits: -4});
  const belt = new THREE.Mesh(shape, mat);
  belt.rotation.x = -Math.PI / 2;
  belt.position.y = 0.09;
  g.add(belt);
  const rail = std(COL.steel, {roughness: 0.5});
  for (const rr of [R - 0.58, R + 0.58]) {
    const t = new THREE.Mesh(new THREE.TorusGeometry(rr, 0.05, 8, 120, a0 - a1), rail);
    t.rotation.x = -Math.PI / 2;
    t.rotation.z = Math.PI / 2 - a0;
    t.position.y = 0.11;
    t.castShadow = true;
    g.add(t);
  }
  return {g, mat};
}

// a station's frame: origin on the belt, local +x along the travel, +z outward from the nest's centre
const stationGroup = (deg) => {
  const g = new THREE.Group();
  g.position.copy(ringPos(deg));
  g.rotation.y = deg * D2R;
  return g;
};

export function buildFactory() {
  const root = new THREE.Group();
  const steel = std(COL.steel, {metalness: 0.35, roughness: 0.5});
  const dark = std(COL.dark, {metalness: 0.2});
  const ash = std(COL.ash, {roughness: 0.7, metalness: 0.02});
  const paper = std('#c9c4cf', {roughness: 0.85, metalness: 0});
  const anchors = {};

  const clip = new THREE.Plane(new V3(0, 0, -1), 99); // the cutaway: 99 keeps the whole nest, 2.2 cuts its front off
  root.add(buildNest(clip));
  const floor = new THREE.Mesh(new THREE.CircleGeometry(40, 64), std('#120d1d', {roughness: 0.95, metalness: 0, envMapIntensity: 0.2}));
  floor.rotation.x = -Math.PI / 2;
  floor.receiveShadow = true;
  root.add(floor);
  const bowl = new THREE.Mesh(new THREE.CircleGeometry(6.6, 96), std('#1a1624', {roughness: 0.9, metalness: 0, polygonOffset: true, polygonOffsetFactor: -1, polygonOffsetUnits: -2}));
  bowl.rotation.x = -Math.PI / 2;
  bowl.position.y = 0.03;
  bowl.receiveShadow = true;
  root.add(bowl);
  const belt = buildBelt();
  root.add(belt.g);

  // ---------------------------------------------- webhook: a hopper over the belt
  const intake = stationGroup(AT.intake);
  root.add(intake);
  {
    const hop = mesh(new THREE.CylinderGeometry(0.72, 0.34, 0.8, 24, 1, true), std(COL.steel, {side: THREE.DoubleSide, metalness: 0.35}), 0, 2.85, 0);
    intake.add(hop, box(0.14, 3.2, 0.14, steel, 0.0, 1.6, 0.86), box(0.14, 0.14, 0.86, steel, 0, 3.15, 0.43));
    anchors.intake = new V3(0, 3.5, 0);
  }
  const lampIntake = lampMat(COL.amber);
  intake.add(mesh(new THREE.SphereGeometry(0.1, 16, 12), lampIntake, 0, 3.32, 0.86));

  // ---------------------------------------------- planner: a press
  const plan = stationGroup(AT.plan);
  root.add(plan);
  const lampPlan = lampMat(COL.amber);
  const head = new THREE.Group();
  {
    for (const x of [-0.82, 0.82]) plan.add(box(0.2, 2.6, 0.5, steel, x, 1.3, 0));
    plan.add(box(2, 0.32, 0.72, dark, 0, 2.66, 0));
    head.add(box(1.25, 0.2, 1.0, std(COL.ash, {roughness: 0.5, metalness: 0.2}), 0, 0, 0), cyl(0.09, 1.1, steel, 0, 0.6, 0));
    const pad = box(1.1, 0.03, 0.86, lampPlan, 0, -0.115, 0);
    pad.castShadow = false;
    head.add(pad);
    plan.add(head);
    anchors.plan = new V3(0, 3, 0);
  }

  // ---------------------------------------------- the gate: bars only a human comment lifts
  const gate = stationGroup(AT.gate);
  root.add(gate);
  const bars = new THREE.Group();
  const lampGate = lampMat(COL.scorch);
  const lampGateOk = lampMat(COL.green);
  {
    for (const z of [-0.78, 0.78]) gate.add(box(0.36, 3.4, 0.36, dark, 0, 1.7, z));
    gate.add(box(0.5, 0.3, 2.1, steel, 0, 3.5, 0));
    const barMat = std(COL.ash, {metalness: 0.3, roughness: 0.45});
    for (let i = 0; i < 5; i++) bars.add(box(0.1, 0.11, 1.26, barMat, 0, 0.28 + i * 0.34, 0));
    for (const z of [-0.36, 0.36]) bars.add(box(0.12, 1.55, 0.08, barMat, 0, 0.98, z));
    gate.add(bars);
    gate.add(mesh(new THREE.SphereGeometry(0.15, 18, 14), lampGate, 0, 3.86, -0.42), mesh(new THREE.SphereGeometry(0.15, 18, 14), lampGateOk, 0, 3.86, 0.42));
    anchors.gate = new V3(0, 4.1, 0);
  }
  const flashGate = new THREE.Mesh(new THREE.PlaneGeometry(1.3, 2), new THREE.MeshBasicMaterial({color: COL.scorch, transparent: true, opacity: 0, side: THREE.DoubleSide, depthWrite: false, blending: THREE.AdditiveBlending, toneMapped: false}));
  flashGate.rotation.y = Math.PI / 2;
  flashGate.position.set(0, 1.05, 0);
  gate.add(flashGate);

  // ---------------------------------------------- the task dock: a low bench the sled parks on
  const dock = stationGroup(AT.dock);
  root.add(dock);
  dock.add(box(1.5, 0.08, 1.24, dark, 0, 0.13, 0));
  anchors.dock = new V3(0, 2.2, 0.5);

  // ---------------------------------------------- after steps: an arch with one lamp per step
  const afterG = stationGroup(AT.after);
  root.add(afterG);
  const stepLamps = [];
  for (const z of [-0.74, 0.74]) afterG.add(box(0.2, 2.1, 0.2, steel, 0, 1.05, z));
  afterG.add(box(0.28, 0.22, 1.7, dark, 0, 2.18, 0));
  for (let i = 0; i < MAX_STEPS; i++) {
    const m = lampMat(COL.amber);
    stepLamps.push(m);
    afterG.add(mesh(new THREE.SphereGeometry(0.075, 12, 10), m, 0.15, 2.18, -0.52 + i * 0.26, false));
  }
  anchors.after = new V3(0, 2.6, 0);

  // ---------------------------------------------- reviewer: a scanner arch with a beam
  const review = stationGroup(AT.review);
  root.add(review);
  const lampOk = lampMat(COL.green);
  const lampBad = lampMat(COL.scorch);
  const beam = new THREE.Mesh(new THREE.PlaneGeometry(1.4, 2.4), new THREE.MeshBasicMaterial({color: COL.blue, transparent: true, opacity: 0, side: THREE.DoubleSide, depthWrite: false, blending: THREE.AdditiveBlending, toneMapped: false}));
  beam.rotation.y = Math.PI / 2;
  {
    for (const z of [-0.84, 0.84]) review.add(box(0.28, 2.9, 0.28, steel, 0, 1.45, z));
    const top = mesh(new THREE.BoxGeometry(0.46, 0.34, 2.05), dark, 0, 3, 0);
    review.add(top, beam);
    beam.position.set(0, 1.35, 0);
    review.add(mesh(new THREE.SphereGeometry(0.14, 16, 12), lampOk, 0, 3.32, -0.36), mesh(new THREE.SphereGeometry(0.14, 16, 12), lampBad, 0, 3.32, 0.36));
    anchors.review = new V3(0, 3.6, 0);
  }

  // ---------------------------------------------- push: a hood at the belt's end, the PR on its stand, the locked merge
  const push = stationGroup(AT.push);
  root.add(push);
  const lampPush = lampMat(COL.blue);
  {
    const hood = mesh(new THREE.CylinderGeometry(0.85, 0.85, 1.5, 28, 1, true, Math.PI / 2, Math.PI), std(COL.steel, {side: THREE.DoubleSide, metalness: 0.35}), 0, 0.1, 0);
    hood.rotation.z = Math.PI / 2;
    hood.rotation.y = 0;
    push.add(hood);
    const lip = box(0.1, 0.1, 1.72, lampPush, 0.76, 0.92, 0);
    lip.castShadow = false;
    push.add(lip);
    anchors.push = new V3(0, 1.4, 0);
  }
  // the stand and the door sit outside the rim, by the belt's end
  const pr = new THREE.Group();
  const prPos = ringPos(AT.push, 8.6);
  pr.position.copy(prPos);
  pr.rotation.y = 18 * D2R;
  root.add(pr);
  const prCard = new THREE.Group();
  const lampPr = lampMat(COL.amber);
  {
    pr.add(mesh(new THREE.CylinderGeometry(0.72, 0.86, 0.36, 28), dark, 0, 0.18, 0));
    prCard.add(box(1.24, 1.56, 0.07, paper, 0, 0, 0));
    for (let i = 0; i < 5; i++) prCard.add(box(0.86 - (i % 2) * 0.22, 0.055, 0.02, std('#4b4658'), -0.04, 0.46 - i * 0.2, 0.04));
    const tag = box(0.86, 0.16, 0.02, lampPr, -0.05, 0.62, 0.05);
    tag.castShadow = false;
    prCard.add(tag);
    pr.add(prCard);
    anchors.pr = new V3(0, 2.8, 0);
  }
  // the wire from the hood to the stand: the branch leaves on it, a dot of the Electric Current's blue
  const wireCurve = new THREE.CatmullRomCurve3([ringPos(AT.push, R, 0.5), ringPos(AT.push, 6.2, 2.2), ringPos(AT.push, 7.7, 1.9), prPos.clone().setY(0.42)]);
  const wireMat = new THREE.MeshBasicMaterial({color: '#0f3d63', toneMapped: false});
  const wire = new THREE.Mesh(new THREE.TubeGeometry(wireCurve, 48, 0.035, 8), wireMat);
  root.add(wire);
  const wireDot = new THREE.Mesh(new THREE.SphereGeometry(0.11, 14, 10), new THREE.MeshBasicMaterial({color: '#5ab8ff', toneMapped: false}));
  wireDot.visible = false;
  root.add(wireDot);
  const door = new THREE.Group();
  door.position.set(1.72, 0, -0.55);
  door.rotation.y = -18 * D2R;
  pr.add(door);
  {
    door.add(box(0.16, 2.5, 0.16, steel, -0.72, 1.25, 0), box(0.16, 2.5, 0.16, steel, 0.72, 1.25, 0), box(1.6, 0.16, 0.16, steel, 0, 2.5, 0));
    door.add(box(1.28, 2.3, 0.08, dark, 0, 1.2, 0));
    const lock = new THREE.Group();
    lock.position.set(0, 1.2, 0.12);
    lock.add(box(0.34, 0.28, 0.1, ash, 0, 0, 0));
    const shackle = new THREE.Mesh(new THREE.TorusGeometry(0.11, 0.03, 8, 20, Math.PI), ash);
    shackle.position.y = 0.14;
    lock.add(shackle);
    door.add(lock);
    anchors.merge = new V3(0, 2.9, 0);
  }

  // ---------------------------------------------- the worktree: a glass wall the agent cannot pass, an egg per task
  const wtPos = new V3(0, 0, 0.1);
  const wt = new THREE.Group();
  wt.position.copy(wtPos);
  root.add(wt);
  const wtFloor = new THREE.Mesh(new THREE.CircleGeometry(WALL, 72), std(COL.panel, {roughness: 0.8, polygonOffset: true, polygonOffsetFactor: -3, polygonOffsetUnits: -6}));
  wtFloor.rotation.x = -Math.PI / 2;
  wtFloor.position.y = 0.07;
  wtFloor.receiveShadow = true;
  wt.add(wtFloor);
  const wallMat = new THREE.ShaderMaterial({
    vertexShader: `varying vec3 vN; varying vec3 vV; varying vec3 vW; void main(){ vec4 mv = modelViewMatrix * vec4(position, 1.0); vN = normalize(normalMatrix * normal); vV = normalize(-mv.xyz); vW = position; gl_Position = projectionMatrix * mv; }`,
    fragmentShader: `uniform float uA; uniform float uFlash; uniform vec3 uHit; varying vec3 vN; varying vec3 vV; varying vec3 vW;
      void main(){ float f = pow(clamp(1.0 - abs(dot(normalize(vN), normalize(vV))), 0.0, 1.0), 2.2);
        float d = length(vW - uHit);
        float h = uFlash * (1.0 - smoothstep(0.1, 2.2, d));
        vec3 base = mix(vec3(0.22, 0.45, 0.85), vec3(0.45, 0.25, 0.95), clamp(vW.y * 0.6, 0.0, 1.0));
        gl_FragColor = vec4(base * (0.28 + 1.05 * f) + vec3(1.0, 0.26, 0.15) * h * 1.3, uA * (0.05 + 0.5 * f) + h * 0.65); }`,
    uniforms: {uA: {value: 1}, uFlash: {value: 0}, uHit: {value: new V3()}},
    transparent: true,
    depthWrite: false,
    side: THREE.DoubleSide,
    toneMapped: false,
  });
  const wall = new THREE.Mesh(new THREE.CylinderGeometry(WALL, WALL, 1.9, 96, 1, true), wallMat);
  wall.position.y = 1.0;
  wall.renderOrder = 8;
  wt.add(wall);
  const wallRim = std(COL.steel, {metalness: 0.4, roughness: 0.45});
  for (const y of [0.08, 1.95]) {
    const rim = new THREE.Mesh(new THREE.TorusGeometry(WALL, y < 1 ? 0.06 : 0.035, 8, 96), wallRim);
    rim.rotation.x = Math.PI / 2;
    rim.position.y = y;
    rim.castShadow = true;
    wt.add(rim);
  }
  anchors.worktree = new V3(wtPos.x, 2.5, wtPos.z);

  // eggs: lower shell + lid, one chick that visits whichever egg is at work
  const profile = (from, to) => {
    const pts = [];
    for (let i = 0; i <= 18; i++) {
      const u = from + ((to - from) * i) / 18;
      const a = u * Math.PI;
      const r = 0.2 * Math.sin(a) * (1 + 0.16 * Math.cos(a));
      pts.push(new THREE.Vector2(Math.max(0.0001, r), 0.27 * -Math.cos(a)));
    }
    return new THREE.LatheGeometry(pts, 24);
  };
  const lowerGeo = profile(0, 0.58);
  const lidGeo = profile(0.58, 1);
  const eggs = [];
  const eggSpot = (i) => {
    // rows of an arc inside the wall, the first task front and centre
    const row = i < 7 ? 0 : i < 12 ? 1 : 2;
    const inRow = [7, 5, 3][row];
    const i0 = [0, 7, 12][row];
    const a = -66 + ((i - i0 + 0.5) / inRow) * 132;
    const r = [1.72, 1.12, 0.52][row];
    return new V3(r * Math.sin(a * D2R), 0.34, r * Math.cos(a * D2R));
  };
  for (let i = 0; i < MAX_TASKS; i++) {
    const m = std('#d9d5dc', {emissive: new THREE.Color(COL.scorch), emissiveIntensity: 0, roughness: 0.55, metalness: 0});
    const g = new THREE.Group();
    const lower = mesh(lowerGeo, m);
    const lid = mesh(lidGeo, m);
    g.add(lower, lid);
    g.position.copy(eggSpot(i));
    g.visible = false;
    wt.add(g);
    eggs.push({g, lid, m, spot: eggSpot(i)});
  }
  const chick = new THREE.Group();
  {
    const chickMat = std('#b9b4c0', {roughness: 0.8, metalness: 0});
    chick.add(mesh(new THREE.SphereGeometry(0.11, 16, 12), chickMat, 0, 0, 0));
    const beak = mesh(new THREE.ConeGeometry(0.035, 0.13, 10), std(COL.amber, {emissive: new THREE.Color(COL.amber), emissiveIntensity: 0.6}), 0, 0.03, 0.12);
    beak.rotation.x = Math.PI / 2 - 0.6;
    chick.add(beak);
  }
  chick.visible = false;
  wt.add(chick);
  const spark = new THREE.Mesh(new THREE.SphereGeometry(0.075, 12, 10), new THREE.MeshBasicMaterial({color: '#ff8a5c', toneMapped: false}));
  spark.visible = false;
  wt.add(spark);
  const glow = new THREE.PointLight(COL.scorch, 0, 3.4, 1.6);
  wt.add(glow);

  // ---------------------------------------------- the sled: the issue, its tasks.md and its commits
  const sled = new THREE.Group();
  root.add(sled);
  sled.add(box(1.1, 0.1, 1.0, dark, 0, 0.17, 0));
  const card = box(0.96, 0.03, 0.82, paper, 0, 0.25, 0);
  sled.add(card);
  const flagMat = lampMat(COL.amber);
  const flag = box(0.42, 0.2, 0.02, flagMat, -0.28, 1.0, 0.42);
  flag.castShadow = false;
  sled.add(box(0.03, 0.8, 0.03, steel, -0.5, 0.65, 0.42), flag);
  flag.position.x = -0.27;
  const rowGeo = new THREE.BoxGeometry(0.07, 0.016, 0.042);
  const rowMat = new THREE.MeshStandardMaterial({roughness: 0.6});
  const rows = new THREE.InstancedMesh(rowGeo, rowMat, MAX_TASKS);
  rows.castShadow = false;
  rows.instanceColor = new THREE.InstancedBufferAttribute(new Float32Array(MAX_TASKS * 3), 3);
  sled.add(rows);
  const lineMat = std('#4b4658');
  const lines = new THREE.InstancedMesh(new THREE.BoxGeometry(0.3, 0.008, 0.016), lineMat, MAX_TASKS);
  sled.add(lines);
  const beadMat = std(COL.blue, {emissive: new THREE.Color('#0d5f9c'), emissiveIntensity: 0.9, roughness: 0.4});
  const beads = new THREE.InstancedMesh(new THREE.CylinderGeometry(0.085, 0.085, 0.05, 16), beadMat, MAX_TASKS + MAX_STEPS);
  sled.add(beads);
  const rowSpot = (i, n) => {
    const col = n > 8 && i >= Math.ceil(n / 2) ? 1 : 0;
    const per = n > 8 ? Math.ceil(n / 2) : n;
    const j = col ? i - per : i;
    const step = Math.min(0.14, 0.64 / Math.max(1, per - 1 || 1));
    return new V3(-0.4 + col * 0.46, 0.272, -0.31 + j * step);
  };

  // ---------------------------------------------- the heron: stands between the belt and the wall, by the dock
  const heron = new THREE.Group();
  const heronBase = ringPos(AT.dock, 3.22);
  heron.position.copy(heronBase);
  root.add(heron);
  const bird = std(COL.ash, {roughness: 0.72, metalness: 0.02});
  const body = mesh(new THREE.SphereGeometry(0.46, 28, 20), bird, 0, 1.62, 0);
  body.scale.set(0.72, 0.8, 1.32);
  body.rotation.x = -0.5;
  const tail = mesh(new THREE.ConeGeometry(0.26, 1.05, 14), bird, 0, 1.22, -0.68);
  tail.scale.set(1, 1, 0.36);
  tail.rotation.x = -2.5;
  heron.add(body, tail);
  const legMat = std('#8d8894', {roughness: 0.55});
  for (const x of [-0.13, 0.13]) {
    heron.add(mesh(new THREE.CylinderGeometry(0.028, 0.032, 1.4, 10), legMat, x, 0.72, 0));
    const toe = box(0.05, 0.03, 0.36, legMat, x, 0.05, 0.1);
    heron.add(toe);
  }
  // the neck: spheres along a curve from shoulder to head, so it meets any target with no joints to solve
  const NECK = 72;
  const neck = new THREE.InstancedMesh(new THREE.SphereGeometry(1, 12, 10), bird, NECK);
  neck.castShadow = true;
  neck.frustumCulled = false;
  heron.add(neck);
  const headG = new THREE.Group();
  heron.add(headG);
  {
    const skull = mesh(new THREE.SphereGeometry(0.105, 16, 12), bird, 0, 0, 0);
    skull.scale.set(0.85, 0.85, 1.18);
    const bill = mesh(new THREE.ConeGeometry(0.052, 0.56, 12), std('#efe9e0', {roughness: 0.5}), 0, 0, 0.36);
    bill.rotation.x = Math.PI / 2;
    const crest = mesh(new THREE.ConeGeometry(0.03, 0.36, 8), std('#57525e'), 0, 0.09, -0.2);
    crest.rotation.x = -Math.PI / 2 - 0.45;
    const eye = mesh(new THREE.SphereGeometry(0.02, 8, 6), std('#110d18', {roughness: 0.2}), 0.078, 0.025, 0.07, false);
    const eye2 = eye.clone();
    eye2.position.x = -0.078;
    headG.add(skull, bill, crest, eye, eye2);
  }
  const carried = box(0.15, 0.03, 0.1, std(COL.ash, {emissive: new THREE.Color(COL.amber), emissiveIntensity: 1}), 0, 0, 0);
  carried.visible = false;
  heron.add(carried);
  anchors.heron = heronBase.clone().setY(3.5);

  // ------------------------------------------------------------ pose
  const mat4 = new THREE.Matrix4();
  const q = new THREE.Quaternion();
  const scl = new V3();
  const tmp = new V3();
  const p0 = new V3();
  const c1 = new V3();
  const c2 = new V3();
  const p3 = new V3();
  const cl = new THREE.Color();
  const look = new THREE.Object3D();
  const sledPos = (a) => ringPos(a);

  const PHASE_TINT = {'rookery:error': COL.scorch, 'rookery:done': COL.green, 'rookery:changes-wanted': COL.amber};

  const update = (s, opts) => {
    const T = opts.T;
    const flowOn = s.moving ? 1 : 0;
    belt.mat.uniforms.uFlow.value = flowOn;
    belt.mat.uniforms.uRun.value = -s.ang * 0.16 * Math.PI;

    // the sled
    const p = sledPos(s.ang);
    sled.position.set(p.x, s.drop * 2.5, p.z);
    sled.rotation.y = s.ang * D2R;
    const pushed = s.pushing;
    sled.visible = s.prUp < 0.5;
    if (pushed > 0) sled.scale.setScalar(1 - pushed * 0.2);
    else sled.scale.setScalar(1);
    cl.set(PHASE_TINT[s.phase] || COL.amber);
    flagMat.emissive.copy(cl);
    flagMat.emissiveIntensity = 0.95;

    // tasks.md on the card
    const n = s.run.n;
    rows.count = s.planOn > 0 ? n : 0;
    lines.count = rows.count;
    for (let i = 0; i < rows.count; i++) {
      const at = rowSpot(i, n);
      const appear = smooth(s.planOn * n - i);
      scl.setScalar(appear || 0.001);
      mat4.compose(at, q.identity(), scl);
      rows.setMatrixAt(i, mat4);
      tmp.copy(at);
      tmp.x += 0.21;
      tmp.y -= 0.006;
      mat4.compose(tmp, q.identity(), scl);
      lines.setMatrixAt(i, mat4);
      const ticked = i < s.ticks;
      const running = i === s.task && !ticked;
      if (s.fail && running) rows.setColorAt(i, cl.set(COL.scorch));
      else if (ticked) rows.setColorAt(i, cl.set(COL.green));
      else if (running) rows.setColorAt(i, cl.set(COL.amber));
      else rows.setColorAt(i, cl.set('#4b4658'));
    }
    rows.instanceMatrix.needsUpdate = true;
    lines.instanceMatrix.needsUpdate = true;
    rows.instanceColor.needsUpdate = true;
    // the commit beads stack up behind the card
    beads.count = Math.min(beads.instanceMatrix.count, s.commits);
    for (let i = 0; i < beads.count; i++) {
      tmp.set(0.34, 0.3 + i * 0.058, 0.32);
      mat4.compose(tmp, q.identity(), scl.setScalar(1));
      beads.setMatrixAt(i, mat4);
    }
    beads.instanceMatrix.needsUpdate = true;

    // hopper, press, gate, arches
    lampIntake.emissiveIntensity = s.key === 'trigger' || s.key === 'pin' ? 1.2 : 0.05;
    head.position.y = 2.28 - s.press * 1.72;
    lampPlan.emissiveIntensity = s.press > 0 ? 1.1 * (0.5 + 0.5 * s.press) : 0;
    bars.position.y = s.gate * 2.05;
    const deny = opts.deny;
    lampGate.emissiveIntensity = s.gateShut ? 0.9 + 1.2 * deny : 0.03;
    lampGateOk.emissiveIntensity = s.gateShut ? 0.03 : 1.0;
    flashGate.material.opacity = deny * 0.55;
    stepLamps.forEach((m, i) => {
      const used = i < s.run.steps;
      m.emissiveIntensity = i === s.step ? 1.3 : used && (s.step > i || ['move', 'review', 'verdict', 'push', 'pr', 'end'].includes(s.key) && s.ang < AT.after) ? 0.45 : 0.02;
    });
    beam.material.opacity = s.scan > 0 ? 0.26 + 0.1 * Math.sin(T * 9) : 0;
    beam.position.x = s.scan > 0 ? -0.6 + 1.2 * (0.5 - 0.5 * Math.cos(s.scan * Math.PI * 4)) : 0;
    lampOk.emissiveIntensity = s.verdict === 'ok' ? s.verdictOn * 1.3 : 0.03;
    lampBad.emissiveIntensity = s.verdict === 'bad' ? s.verdictOn * 1.3 : 0.03;
    lampPush.emissiveIntensity = s.key === 'push' ? 1.3 : s.pushing ? 0.3 : 0.03;

    wireDot.visible = s.key === 'push';
    if (wireDot.visible) wireCurve.getPoint(s.k, wireDot.position);
    wireMat.color.set(s.key === 'push' ? '#2f8fd6' : s.pushing ? '#1a5e94' : '#0f3d63');

    // the draft PR
    prCard.position.y = 0.36 - 0.9 + s.prUp * 1.75;
    prCard.visible = s.prUp > 0.01;
    const prDrift = s.prUp >= 1 ? Math.sin(T * 1.4) * 0.03 : 0;
    prCard.position.y += prDrift;
    lampPr.emissiveIntensity = 0.9;
    cl.set(PHASE_TINT[s.phase] || COL.amber);
    lampPr.emissive.copy(cl);

    // the worktree
    clip.constant = opts.cut > 0.5 ? 2.2 : 99;
    wallMat.uniforms.uA.value = 1 - opts.noWall * 0.92;
    wall.visible = opts.noWall < 0.99;
    let activeSpot = null;
    eggs.forEach((e, i) => {
      const out = i <= s.task || i < s.ticks;
      const inUse = i < s.run.n && out;
      e.g.visible = inUse;
      if (!inUse) return;
      const current = i === s.task && i >= s.ticks;
      const pop = current ? (s.hatch > 0 ? s.hatch : 1) : 1;
      e.g.scale.setScalar(0.3 + 0.7 * pop);
      const k = current ? (s.working ? 1 : 0) : 0;
      e.lid.position.y = k * 0.17;
      e.lid.rotation.z = k * 0.42;
      e.lid.position.x = k * 0.12;
      e.m.emissiveIntensity = current ? (s.fail ? 0.8 + 0.35 * Math.sin(T * 12) : s.working ? 0.42 + 0.14 * Math.sin(T * 6) : 0.15) : 0;
      e.m.color.set(current ? '#d9d5dc' : '#46414f');
      if (current) activeSpot = e.spot;
    });
    glow.intensity = activeSpot && s.working ? 3.2 : activeSpot && s.fail ? 5 : 0;
    if (activeSpot) glow.position.set(activeSpot.x, 0.95, activeSpot.z);
    chick.visible = !!(activeSpot && s.working);
    if (chick.visible) {
      const peck = Math.max(0, Math.sin(T * 8)) * 0.05;
      chick.position.set(activeSpot.x, activeSpot.y + 0.12 - peck, activeSpot.z);
      chick.rotation.x = peck * 4;
    }
    // what the agent tries: a spark from the egg to the wall, and back
    const g = opts.guard;
    const hitAt = 0.42;
    spark.visible = false;
    wallMat.uniforms.uFlash.value = 0;
    if (g) {
      const from = activeSpot || eggs[0].spot;
      const dir = tmp.set(from.x, 0, from.z);
      if (dir.lengthSq() < 0.01) dir.set(0, 0, 1);
      dir.normalize();
      const hit = dir.clone().multiplyScalar(WALL - 0.03).setY(1.0);
      wallMat.uniforms.uHit.value.copy(hit).setY(0);
      if (g.age < hitAt) {
        spark.visible = true;
        spark.position.lerpVectors(new V3(from.x, 0.75, from.z), hit, g.age / hitAt);
      } else if (g.age < hitAt + 0.42) {
        spark.visible = true;
        spark.position.lerpVectors(hit, new V3(from.x, 0.75, from.z), (g.age - hitAt) / 0.42);
      }
      if (g.age >= hitAt) wallMat.uniforms.uFlash.value = Math.exp(-(g.age - hitAt) * 2.6);
    }

    // the heron: head and bill on a target, from run.js's beats
    const cardAt = (i) => sled.localToWorld(rowSpot(Math.max(0, i), n).clone()).sub(heronBase);
    sled.updateMatrixWorld();
    const eggAt = (i) => eggs[Math.max(0, Math.min(MAX_TASKS - 1, i))].spot.clone().add(wtPos).setY(0.75).sub(heronBase);
    const atDock = s.station === 'dock' && !s.moving;
    const rest = new V3(Math.sin(T * 0.7) * 0.25, 2.95 + Math.sin(T * 1.1) * 0.05, 0.6);
    const towards = sledPos(s.ang).sub(heronBase).setY(0);
    const lookAtSled = towards.clone().normalize().multiplyScalar(Math.min(1.3, towards.length())).setY(2.7);
    let tgt = lookAtSled;
    let aim = null;
    carried.visible = false;
    if (atDock) {
      if (s.key === 'hatch') {
        // the bill carries the task from the card to its egg
        const a = cardAt(s.task);
        const b = eggAt(s.task);
        const k = smooth(s.k * 1.2);
        tgt = a.clone().lerp(b, k);
        tgt.y += Math.sin(k * Math.PI) * 1.5;
        carried.visible = true;
      } else if (s.key === 'work' || s.key === 'failed') {
        tgt = eggAt(s.task).multiplyScalar(0.55).setY(2.5);
        aim = eggAt(s.task);
      } else if (s.key === 'status') {
        tgt = eggAt(s.task).setY(1.5);
      } else if (s.key === 'tick') {
        const a = eggAt(s.task).setY(1.5);
        const b = cardAt(s.task).setY(0.5);
        tgt = a.lerp(b, smooth(s.k * 2));
        tgt.y -= Math.max(0, Math.sin(Math.max(0, s.k - 0.45) * 10)) * 0.2;
      } else if (s.key === 'commit') {
        const b = sled.localToWorld(new V3(0.34, 0.5 + s.commits * 0.06, 0.32)).sub(heronBase);
        const a = cardAt(s.task).setY(0.5);
        tgt = a.lerp(b, smooth(s.k * 1.6));
      } else {
        tgt = cardAt(s.task < 0 ? 0 : s.task).setY(1.2);
      }
    } else if (s.key === 'end' || s.key === 'pr') {
      tgt = rest;
    } else if (s.key !== 'move') {
      tgt = lookAtSled;
    }
    aim = aim || tgt.clone().setY(tgt.y - 0.25);
    p3.copy(tgt);
    // the bill's tip is on the target: back the head off along the look direction
    const dirH = aim.clone().sub(tgt);
    if (dirH.lengthSq() < 1e-4) dirH.set(0, -1, 0.2);
    dirH.normalize();
    const shoulder = p0.set(0, 1.86, 0.3);
    const facing = new V3(tgt.x, 0, tgt.z);
    if (facing.lengthSq() < 0.01) facing.set(0, 0, 1);
    facing.normalize();
    // the body turns toward the work, a little
    heron.rotation.y = 0;
    p3.copy(tgt).addScaledVector(dirH, -0.55);
    // an S curve: back from the shoulder first, then forward over the head
    c1.copy(shoulder).addScaledVector(facing, -0.42).setY(2.72 + Math.max(0, p3.y - 1.5) * 0.25);
    c2.copy(p3).addScaledVector(facing, -0.55).setY(Math.max(p3.y + 0.75, 2.65));
    const NR0 = 0.13;
    const NR1 = 0.058;
    for (let i = 0; i < NECK; i++) {
      const u = i / (NECK - 1);
      const m = 1 - u;
      tmp.set(0, 0, 0).addScaledVector(shoulder, m * m * m).addScaledVector(c1, 3 * m * m * u).addScaledVector(c2, 3 * m * u * u).addScaledVector(p3, u * u * u);
      scl.setScalar(NR0 + (NR1 - NR0) * Math.pow(u, 0.7));
      mat4.compose(tmp, q.identity(), scl);
      neck.setMatrixAt(i, mat4);
    }
    neck.instanceMatrix.needsUpdate = true;
    headG.position.copy(p3);
    look.position.copy(p3);
    look.lookAt(p3.clone().add(dirH));
    headG.quaternion.copy(look.quaternion);
    carried.position.copy(tgt);
    carried.quaternion.copy(look.quaternion);
  };

  const pos = {
    intake: ringPos(AT.intake),
    plan: ringPos(AT.plan),
    gate: ringPos(AT.gate),
    dock: ringPos(AT.dock),
    after: ringPos(AT.after),
    review: ringPos(AT.review),
    push: ringPos(AT.push),
    pr: prPos.clone(),
    worktree: wtPos.clone(),
    heron: heronBase.clone(),
  };
  return {root, update, anchors, pos, stationRot: {intake, plan, gate, dock, after: afterG, review, push}, pr, door, sled, eggs, wt};
}
