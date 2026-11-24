/* ACM Systems · maqueta 3D del circuito de la operación.
   Seis estaciones sobre un riel circular, unidas por radios a un núcleo: el sistema único. Una unidad recorre
   el circuito y cambia de estado: necesidad (contorno) → pedido (ámbar) → caja con lote → venta → pago.
   Geometría procedimental: no descarga modelos ni texturas. La página decide qué estación mostrar; este módulo
   solo dibuja. No lee ni escribe datos. */
import * as THREE from '../vendor/three-0.187.0/three.module.min.js';

const C = {
  night: 0x0f1719, plate: 0x14232a, plateTop: 0x192b32, pad: 0x223840, groove: 0x16242a, steel: 0x587177, steelLight: 0x93adb1,
  paper: 0xf1f5ef, ink: 0x1b2e2e, kraft: 0xbf9a68, kraftDark: 0x8c6c45,
  mint: 0x9ee6ce, mintDeep: 0x45b592, amber: 0xffbe74, coral: 0xed9b86,
};
const TAU = Math.PI * 2;
const STEP = TAU / 6;
const A0 = Math.PI * 2 / 3;          // las dos primeras estaciones quedan al frente, a cada lado del núcleo
const R = 4;                          // radio del riel
const NAMES = ['Solicitud', 'Inventario', 'Compra', 'Recepción', 'Venta', 'Cobro'];
const ACCENT = [C.mint, C.mint, C.amber, C.mint, C.mint, C.amber];

const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
const lerp = (a, b, t) => a + (b - a) * t;
const sm = (a, b, v) => { const t = clamp((v - a) / (b - a), 0, 1); return t * t * (3 - 2 * t); };
const damp = (cur, target, lambda, dt) => cur + (target - cur) * (1 - Math.exp(-lambda * dt));
const stationAngle = p => A0 - p * STEP;
/* Avance sobre el riel con pausa en cada estación: el tramo entre dos estaciones se recorre suavizado. */
const railParam = u => { const i = Math.floor(u); return i + sm(0.14, 0.86, u - i); };
const cyclic = (a, b) => { const d = Math.abs(a - b) % 6; return Math.min(d, 6 - d); };
const LOOP = 5.2;                     // segundos de cada repetición de la acción de una estación
const replay = clock => { const l = (clock / LOOP) % 1; return l < 0.34 ? 1 : l < 0.42 ? lerp(1, 0.25, sm(0.34, 0.42, l)) : lerp(0.25, 1, sm(0.42, 0.96, l)); };

function mesh(geometry, material, x = 0, y = 0, z = 0, shadow = true) {
  const m = new THREE.Mesh(geometry, material);
  m.position.set(x, y, z);
  m.castShadow = shadow; m.receiveShadow = shadow;
  return m;
}
const box = (w, h, d, material, x, y, z, shadow) => mesh(new THREE.BoxGeometry(w, h, d), material, x, y, z, shadow);
const cyl = (r1, r2, h, seg, material, x, y, z, shadow) => mesh(new THREE.CylinderGeometry(r1, r2, h, seg), material, x, y, z, shadow);
/* Renglón de un documento: crece desde su borde izquierdo. */
function lineBar(width, material, x, y, z) {
  const g = new THREE.BoxGeometry(width, 0.05, 0.012);
  g.translate(width / 2, 0, 0);
  return mesh(g, material, x, y, z, false);
}
function edges(w, h, d, material) {
  return new THREE.LineSegments(new THREE.EdgesGeometry(new THREE.BoxGeometry(w, h, d)), material);
}

function frame(w, h, d, t, material) {
  const group = new THREE.Group();
  for (const sy of [-1, 1]) for (const sz of [-1, 1]) group.add(mesh(new THREE.BoxGeometry(w + t, t, t), material, 0, sy * h / 2, sz * d / 2, false));
  for (const sx of [-1, 1]) for (const sz of [-1, 1]) group.add(mesh(new THREE.BoxGeometry(t, h + t, t), material, sx * w / 2, 0, sz * d / 2, false));
  for (const sx of [-1, 1]) for (const sy of [-1, 1]) group.add(mesh(new THREE.BoxGeometry(t, t, d + t), material, sx * w / 2, sy * h / 2, 0, false));
  return group;
}

function materials() {
  const std = (color, o = {}) => new THREE.MeshStandardMaterial({ color, roughness: 0.72, metalness: 0, ...o });
  const glow = (color, k = 0.9) => std(color, { emissive: color, emissiveIntensity: k, roughness: 0.45 });
  return {
    plate: std(C.plate, { roughness: 0.8 }), plateTop: std(C.plateTop, { roughness: 0.74 }), pad: std(C.pad, { roughness: 0.55, metalness: 0.2 }), groove: std(C.groove, { roughness: 0.9 }),
    steel: std(C.steel, { roughness: 0.42, metalness: 0.55 }), steelLight: std(C.steelLight, { roughness: 0.38, metalness: 0.6 }),
    paper: std(C.paper, { roughness: 0.9 }), ink: std(C.ink, { roughness: 0.8 }),
    kraft: std(C.kraft, { roughness: 0.88 }), kraftDark: std(C.kraftDark, { roughness: 0.9 }),
    mintMetal: std(C.mint, { roughness: 0.3, metalness: 0.5 }), gold: std(C.amber, { roughness: 0.28, metalness: 0.75 }),
    std, glow,
  };
}

/* ---------------------------------------------------------------- texturas pintadas en el navegador */
function canvasTexture(width, height, draw) {
  const canvas = document.createElement('canvas');
  canvas.width = width; canvas.height = height;
  const texture = new THREE.CanvasTexture(canvas);
  texture.colorSpace = THREE.SRGBColorSpace;
  texture.anisotropy = 4;
  const paint = () => { const ctx = canvas.getContext('2d'); ctx.clearRect(0, 0, width, height); draw(ctx, width, height); texture.needsUpdate = true; };
  paint();
  return { texture, paint };
}
const FONT = '"IBM Plex Sans Condensed", "Arial Narrow", Arial, sans-serif';
function floorLabel(order, name) {
  return canvasTexture(640, 160, (ctx, w, h) => {
    ctx.textBaseline = 'middle';
    ctx.font = `600 54px ${FONT}`;
    ctx.fillStyle = 'rgba(158,230,206,.9)';
    ctx.fillText(String(order).padStart(2, '0'), 18, h / 2 + 8);
    ctx.font = `600 92px ${FONT}`;
    ctx.fillStyle = 'rgba(240,248,243,1)';
    ctx.fillText(name.toUpperCase(), 100, h / 2 + 4);
  });
}
function lotLabel() {
  return canvasTexture(256, 160, (ctx, w, h) => {
    ctx.fillStyle = '#f1f5ef'; ctx.fillRect(0, 0, w, h);
    ctx.fillStyle = '#1b2e2e';
    ctx.font = `600 38px ${FONT}`; ctx.textBaseline = 'top';
    ctx.fillText('LOTE', 16, 14);
    ctx.font = `600 62px ${FONT}`;
    ctx.fillText('SL-041', 16, 56);
    for (let i = 0; i < 14; i++) ctx.fillRect(16 + i * 16, 128, i % 3 === 0 ? 9 : 5, 22);
  });
}
function glowTexture() {
  return canvasTexture(128, 128, (ctx, w) => {
    const g = ctx.createRadialGradient(w / 2, w / 2, 0, w / 2, w / 2, w / 2);
    g.addColorStop(0, 'rgba(255,255,255,.9)'); g.addColorStop(0.35, 'rgba(255,255,255,.28)'); g.addColorStop(1, 'rgba(255,255,255,0)');
    ctx.fillStyle = g; ctx.fillRect(0, 0, w, w);
  }).texture;
}

/* ------------------------------------------------------------------------------ las seis estaciones
   Cada una vive en su propio sistema: x sigue al riel, z apunta hacia fuera del circuito e y hacia arriba.
   `update(w, t)` recibe cuánto protagonismo tiene (0–1) y el tiempo en segundos. */
function buildSolicitud(M) {
  const group = new THREE.Group();
  group.add(box(0.5, 0.1, 0.42, M.steel, 0, 0.17, -1.0));
  const board = new THREE.Group();
  board.position.set(0, 0.16, -0.62); board.rotation.x = -0.4;
  group.add(board);
  board.add(box(1.24, 1.62, 0.07, M.steel, 0, 0.84, 0));
  board.add(box(1.04, 1.38, 0.03, M.paper, 0, 0.82, 0.045));
  board.add(box(0.46, 0.14, 0.1, M.mintMetal, 0, 1.6, 0.05));
  const lines = [[1.3, 0.78], [1.12, 0.78], [0.94, 0.62], [0.76, 0.4]].map(([y, w]) => {
    const l = lineBar(w, M.ink, -0.39, y, 0.066); board.add(l); return l;
  });
  const checkMat = M.glow(C.mint, 0.2);
  const check = box(0.22, 0.22, 0.03, checkMat, 0.28, 0.42, 0.07, false);
  board.add(check);
  board.add(lineBar(0.42, M.ink, -0.39, 0.42, 0.066));
  return { group, update(w, t) {
    lines.forEach((l, i) => { l.scale.x = 0.3 + 0.7 * sm(i * 0.16, i * 0.16 + 0.4, w); });
    const done = sm(0.7, 0.95, w);
    check.scale.setScalar(0.55 + 0.45 * done + 0.04 * done * Math.sin(t * 3));
    checkMat.emissiveIntensity = 0.15 + 1.5 * done;
  } };
}

function buildInventario(M) {
  const group = new THREE.Group();
  const rack = new THREE.Group();
  rack.position.set(0, 0.12, -0.72);
  group.add(rack);
  for (const x of [-0.98, 0.98]) for (const z of [-0.3, 0.3]) rack.add(box(0.07, 1.98, 0.07, M.steel, x, 0.99, z));
  const shelves = [0.16, 0.8, 1.44];
  shelves.forEach(y => rack.add(box(2.03, 0.05, 0.68, M.steel, 0, y, 0)));
  const labelMat = M.glow(C.mint, 0.2);
  const firstMat = M.std(C.amber, { emissive: C.amber, emissiveIntensity: 0.15, roughness: 0.7 });
  const gapMat = new THREE.LineBasicMaterial({ color: C.coral, transparent: true, opacity: 0.3 });
  let first = null;
  shelves.forEach((y, row) => [-0.62, 0, 0.62].forEach((x, col) => {
    const index = row * 3 + col;
    if (index === 7) { const gap = edges(0.46, 0.42, 0.46, gapMat); gap.position.set(x, y + 0.235, 0); rack.add(gap); return; }
    const crate = box(0.46, 0.42, 0.46, index === 0 ? firstMat : M.kraft, x, y + 0.235, 0);
    crate.add(box(0.2, 0.11, 0.012, index === 0 ? M.ink : labelMat, -0.08, 0.06, 0.236, false));
    rack.add(crate);
    if (index === 0) first = crate;
  }));
  return { group, update(w, t) {
    labelMat.emissiveIntensity = 0.2 + 1.1 * w;
    gapMat.opacity = 0.3 + w * (0.45 + 0.25 * Math.sin(t * 4));
    firstMat.emissiveIntensity = 0.15 + w * (0.5 + 0.2 * Math.sin(t * 2.4));
    first.position.z = 0.16 * sm(0.5, 0.9, w);     // el lote que vence primero se adelanta: sale primero
  } };
}

function buildCompra(M) {
  const group = new THREE.Group();
  // Estiba con las unidades pedidas: todavía no existen, por eso son contornos.
  const pallet = new THREE.Group();
  pallet.position.set(0.5, 0.12, -0.78);
  group.add(pallet);
  pallet.add(box(1.24, 0.07, 0.96, M.kraftDark, 0, 0.13, 0));
  for (const x of [-0.5, 0, 0.5]) pallet.add(box(0.12, 0.1, 0.96, M.kraftDark, x, 0.05, 0));
  const ghostLine = new THREE.LineBasicMaterial({ color: C.amber, transparent: true, opacity: 0.75 });
  const ghostFill = new THREE.MeshBasicMaterial({ color: C.amber, transparent: true, opacity: 0.04, depthWrite: false });
  [[-0.29, 0.39, -0.22], [0.29, 0.39, -0.22], [-0.29, 0.39, 0.24], [0.29, 0.39, 0.24], [0, 0.85, 0]].forEach(([x, y, z]) => {
    const e = edges(0.5, 0.44, 0.42, ghostLine); e.position.set(x, y, z); pallet.add(e);
    pallet.add(mesh(new THREE.BoxGeometry(0.5, 0.44, 0.42), ghostFill, x, y, z, false));
  });
  // Orden de compra y sello: la aprobación es de una persona.
  const doc = new THREE.Group();
  doc.position.set(-0.78, 0.16, -0.6); doc.rotation.set(-0.36, 0.3, 0);
  group.add(doc);
  doc.add(box(0.9, 1.16, 0.06, M.steel, 0, 0.6, 0));
  doc.add(box(0.76, 1.0, 0.03, M.paper, 0, 0.6, 0.04));
  [[0.98, 0.54], [0.84, 0.54], [0.7, 0.36]].forEach(([y, w]) => doc.add(lineBar(w, M.ink, -0.28, y, 0.06)));
  const stripMat = M.glow(C.amber, 0.5);
  doc.add(box(0.4, 0.09, 0.012, stripMat, -0.08, 0.52, 0.06, false));
  const markMat = new THREE.MeshBasicMaterial({ color: C.mintDeep, transparent: true, opacity: 0 });
  const mark = mesh(new THREE.RingGeometry(0.09, 0.15, 32), markMat, 0.14, 0.26, 0.058, false);
  doc.add(mark);
  const stamp = new THREE.Group();
  stamp.rotation.x = Math.PI / 2;
  stamp.add(cyl(0.16, 0.19, 0.1, 28, M.gold, 0, 0.05, 0));
  stamp.add(cyl(0.055, 0.055, 0.28, 16, M.steelLight, 0, 0.24, 0));
  stamp.add(mesh(new THREE.SphereGeometry(0.1, 20, 14), M.gold, 0, 0.42, 0));
  doc.add(stamp);
  const amber = new THREE.Color(C.amber), mint = new THREE.Color(C.mint);
  return { group, update(w, t) {
    const press = sm(0.42, 0.66, w) - sm(0.76, 0.96, w);
    const approved = sm(0.62, 0.72, w);
    stamp.position.set(0.14, 0.26, lerp(0.62 + 0.03 * Math.sin(t * 2), 0.07, press));
    markMat.opacity = approved;
    stripMat.color.copy(amber).lerp(mint, approved); stripMat.emissive.copy(stripMat.color);
    ghostFill.opacity = 0.04 + 0.14 * approved;
    ghostLine.opacity = 0.28 + 0.22 * w;
  } };
}

function buildRecepcion(M) {
  const group = new THREE.Group();
  group.add(box(2.5, 0.12, 0.84, M.steel, 0, 0.18, 0));
  const rollers = [];
  for (let i = 0; i < 9; i++) {
    const r = cyl(0.055, 0.055, 0.78, 12, M.steelLight, -1.08 + i * 0.27, 0.27, 0, false);
    r.rotation.x = Math.PI / 2; group.add(r); rollers.push(r);
  }
  for (const z of [-0.56, 0.56]) group.add(box(0.1, 1.36, 0.1, M.steel, 0, 0.8, z));
  group.add(box(0.16, 0.12, 1.24, M.steel, 0, 1.52, 0));
  const headMat = M.glow(C.mint, 0.4);
  group.add(box(0.07, 0.035, 0.96, headMat, 0, 1.44, 0, false));
  const laserMat = new THREE.MeshBasicMaterial({ color: C.mint, transparent: true, opacity: 0, side: THREE.DoubleSide, depthWrite: false, blending: THREE.AdditiveBlending });
  const laser = mesh(new THREE.PlaneGeometry(0.94, 1.1), laserMat, 0, 0.88, 0, false);
  laser.rotation.y = Math.PI / 2; group.add(laser);
  // Impresora de etiquetas: la unidad sale de aquí con su lote.
  group.add(box(0.46, 0.4, 0.36, M.plateTop, 0.92, 0.32, -0.86));
  const tag = box(0.3, 0.2, 0.012, M.paper, 0.92, 0.6, -0.72, false);
  tag.rotation.x = -0.5; group.add(tag);
  return { group, laser, update(w, t) {
    headMat.emissiveIntensity = 0.4 + 1.4 * w;
    laserMat.opacity = 0.34 * w;
    laser.position.x = 0.5 * Math.sin(t * 2.2);
    rollers.forEach(r => { r.rotation.y = -t * 2.4 * w; });
  } };
}

function buildVenta(M) {
  const group = new THREE.Group();
  group.add(box(1.76, 0.6, 0.56, M.plateTop, 0, 0.42, -0.86));
  group.add(box(1.84, 0.06, 0.64, M.steel, 0, 0.75, -0.86));
  group.add(box(0.08, 0.3, 0.08, M.steel, -0.5, 0.92, -0.92));
  const screen = new THREE.Group();
  screen.position.set(-0.5, 1.18, -0.9); screen.rotation.x = -0.32;
  group.add(screen);
  screen.add(box(0.62, 0.44, 0.05, M.ink, 0, 0, 0));
  const screenMat = M.glow(C.mint, 0.3);
  screen.add(box(0.5, 0.07, 0.01, screenMat, 0, 0.1, 0.03, false));
  screen.add(box(0.32, 0.07, 0.01, screenMat, -0.09, -0.02, 0.03, false));
  screen.add(box(0.18, 0.07, 0.01, M.glow(C.amber, 0.7), 0.16, -0.14, 0.03, false));
  // La factura sale del mostrador hacia el cliente.
  const invoiceGeo = new THREE.BoxGeometry(0.46, 0.016, 0.7); invoiceGeo.translate(0, 0, 0.35);
  const invoice = mesh(invoiceGeo, M.paper, 0.36, 0.79, -1.1, false);
  [0.12, 0.26, 0.4].forEach(z => invoice.add(box(0.3, 0.004, 0.035, M.ink, -0.03, 0.01, z, false)));
  group.add(invoice);
  const chevMat = new THREE.MeshBasicMaterial({ color: C.mint, transparent: true, opacity: 0.2 });
  const chevrons = [0.72, 1.0, 1.28].map(z => {
    const g = new THREE.Group(); g.position.set(0, 0.125, z);
    for (const s of [-1, 1]) { const b = box(0.34, 0.012, 0.07, chevMat.clone(), s * 0.11, 0, 0, false); b.rotation.y = s * 0.78; g.add(b); }
    group.add(g); return g;
  });
  return { group, update(w, t) {
    screenMat.emissiveIntensity = 0.3 + 1.3 * w;
    invoice.scale.z = 0.25 + 0.75 * sm(0.3, 0.85, w);
    chevrons.forEach((g, i) => {
      const o = 0.14 + w * 0.7 * (0.5 + 0.5 * Math.sin(t * 3.4 - i * 1.1));
      g.children.forEach(c => { c.material.opacity = o; });
    });
  } };
}

function buildCobro(M) {
  const group = new THREE.Group();
  // Banco (simulado en la demostración).
  const bank = new THREE.Group();
  bank.position.set(-0.5, 0.12, -0.84);
  group.add(bank);
  bank.add(box(1.3, 0.12, 0.62, M.steel, 0, 0.06, 0));
  for (const x of [-0.48, -0.16, 0.16, 0.48]) bank.add(cyl(0.065, 0.065, 0.7, 14, M.paper, x, 0.47, 0.12));
  bank.add(box(1.22, 0.62, 0.3, M.plateTop, 0, 0.45, -0.12));
  bank.add(box(1.3, 0.1, 0.62, M.steel, 0, 0.87, 0));
  const roof = cyl(0.76, 0.76, 0.6, 3, M.steel, 0, 1.1, 0);
  roof.rotation.x = -Math.PI / 2; roof.scale.set(1, 1, 0.5); bank.add(roof);
  // El pago encuentra su factura.
  group.add(box(0.98, 0.42, 0.34, M.plateTop, 0.74, 0.33, -0.82));
  const invoice = box(0.36, 0.46, 0.03, M.paper, 0, 0.79, -0.8);
  [0.12, 0.02, -0.08].forEach(y => invoice.add(box(0.24, 0.035, 0.008, M.ink, 0, y, 0.02, false)));
  const payMat = M.glow(C.mint, 0.35);
  const payment = box(0.36, 0.46, 0.03, payMat, 0, 0.79, -0.8);
  group.add(invoice, payment);
  const ringMat = M.glow(C.mint, 0.2);
  const ring = mesh(new THREE.TorusGeometry(0.5, 0.022, 10, 56), ringMat, 0.74, 0.8, -0.8, false);
  group.add(ring);
  const coins = [];
  for (let i = 0; i < 5; i++) { const c = cyl(0.2, 0.2, 0.065, 30, M.gold, 0.96, 0.16 + i * 0.07, -0.26); group.add(c); coins.push(c); }
  return { group, update(w, t) {
    const join = sm(0.4, 0.8, w);
    invoice.position.x = 0.74 - lerp(0.36, 0.19, join);
    payment.position.x = 0.74 + lerp(0.36, 0.19, join);
    const matched = sm(0.8, 0.95, w);
    payMat.emissiveIntensity = 0.35 + 1.1 * matched;
    ringMat.emissiveIntensity = 0.2 + 1.6 * matched;
    ring.scale.setScalar(0.72 + 0.28 * matched + 0.02 * matched * Math.sin(t * 3));
    coins.forEach((c, i) => {
      const k = i < 2 ? 1 : sm((i - 2) * 0.2, (i - 2) * 0.2 + 0.35, w);
      c.position.y = 0.16 + i * 0.07 + (1 - k) * 0.9;
      c.scale.setScalar(0.001 + k);
    });
  } };
}

const BUILDERS = [buildSolicitud, buildInventario, buildCompra, buildRecepcion, buildVenta, buildCobro];

/* --------------------------------------------------------------------------------- núcleo y unidad */
function buildCore(M) {
  const group = new THREE.Group();
  group.add(cyl(1.26, 1.42, 0.5, 6, M.plateTop, 0, 0.37, 0));
  const tileMat = M.std(C.mint, { emissive: C.mint, emissiveIntensity: 0.3, roughness: 0.4 });
  const s = 0.84, r = 0.2;
  const tile = new THREE.Shape();
  tile.moveTo(-s + r, -s); tile.lineTo(s - r, -s); tile.quadraticCurveTo(s, -s, s, -s + r); tile.lineTo(s, s - r);
  tile.quadraticCurveTo(s, s, s - r, s); tile.lineTo(-s + r, s); tile.quadraticCurveTo(-s, s, -s, s - r); tile.lineTo(-s, -s + r);
  tile.quadraticCurveTo(-s, -s, -s + r, -s);
  group.add(cyl(0.11, 0.16, 2.2, 14, M.steel, 0, 1.7, 0));
  group.add(cyl(0.34, 0.4, 0.12, 20, M.steel, 0, 0.68, 0));
  const sign = new THREE.Group();
  sign.position.y = 3.1;
  group.add(sign);
  const flat = new THREE.Group();
  flat.rotation.x = -0.3; flat.position.z = -0.05;
  sign.add(flat);
  flat.add(mesh(new THREE.BoxGeometry(0.5, 0.5, 0.16), M.steel, 0, 0, -0.06));
  flat.add(mesh(new THREE.ExtrudeGeometry(tile, { depth: 0.1, bevelEnabled: true, bevelSize: 0.02, bevelThickness: 0.02, bevelSegments: 2 }), tileMat));
  // Marca de ACM Systems (misma geometría del ícono de la aplicación).
  const k = v => (v - 256) / 512 * 2 * s, y = v => -(v - 256) / 512 * 2 * s;
  const poly = pts => { const sh = new THREE.Shape(); pts.forEach(([px, py], i) => (i ? sh.lineTo(k(px), y(py)) : sh.moveTo(k(px), y(py)))); return sh; };
  const dot = new THREE.Shape(); dot.absarc(k(372), y(355), 27 / 512 * 2 * s, 0, TAU);
  const mark = [poly([[102, 355], [221, 133], [276, 133], [157, 355]]), poly([[246, 355], [331, 196], [388, 196], [303, 355]]), poly([[201, 273], [323, 273], [323, 322], [175, 322]]), dot];
  const markMesh = mesh(new THREE.ExtrudeGeometry(mark, { depth: 0.05, bevelEnabled: false }), M.ink, 0, 0, 0.12, false);
  flat.add(markMesh);
  return { group, tileMat, sign };
}

function buildUnit(M, glowMap) {
  const group = new THREE.Group();
  const ghostLine = new THREE.MeshBasicMaterial({ color: C.mint, transparent: true });
  const ghostFill = new THREE.MeshBasicMaterial({ color: C.mint, transparent: true, opacity: 0.24, depthWrite: false });
  const ghost = new THREE.Group();
  ghost.add(frame(0.5, 0.46, 0.5, 0.04, ghostLine), mesh(new THREE.BoxGeometry(0.5, 0.46, 0.5), ghostFill, 0, 0, 0, false));
  const haloMat = new THREE.SpriteMaterial({ map: glowMap, color: C.mint, transparent: true, opacity: 0.5, depthWrite: false, blending: THREE.AdditiveBlending });
  const halo = new THREE.Sprite(haloMat);
  halo.scale.setScalar(2.3);
  group.add(halo);
  const crate = new THREE.Group();
  crate.add(box(0.5, 0.46, 0.5, M.kraft, 0, 0, 0));
  crate.add(box(0.51, 0.47, 0.09, M.kraftDark, 0, 0, 0, false));
  const label = lotLabel();
  const labelMat = new THREE.MeshBasicMaterial({ map: label.texture });
  for (const s of [-1, 1]) {
    const p = mesh(new THREE.PlaneGeometry(0.3, 0.19), labelMat, 0.06 * s, 0.03, 0.256 * s, false);
    p.rotation.y = s < 0 ? Math.PI : 0; crate.add(p);
  }
  const sealMat = M.glow(C.mint, 0.9);
  const seal = box(0.52, 0.08, 0.52, sealMat, 0, -0.12, 0, false);
  crate.add(seal);
  const coin = new THREE.Group();
  const disc = cyl(0.25, 0.25, 0.07, 40, M.gold, 0, 0, 0);
  disc.rotation.z = Math.PI / 2;
  const rimMat = M.glow(C.mint, 0.8);
  const rim = mesh(new THREE.TorusGeometry(0.25, 0.018, 8, 40), rimMat, 0, 0, 0, false);
  rim.rotation.y = Math.PI / 2;
  coin.add(disc, rim);
  group.add(ghost, crate, coin);
  const mint = new THREE.Color(C.mint), amber = new THREE.Color(C.amber);
  return { group, label, update(u, t) {
    const ordered = sm(1.75, 2.0, u) * (1 - sm(2.9, 3.0, u));
    const solid = sm(2.9, 3.02, u) * (1 - sm(4.78, 4.94, u));
    const paid = sm(4.86, 5.0, u) * (1 - sm(5.3, 5.5, u));
    const need = (1 - sm(2.86, 3.0, u)) + sm(5.6, 5.9, u);
    ghost.visible = need > 0.01;
    ghostLine.opacity = need; ghostFill.opacity = need * (0.24 + 0.2 * sm(2.55, 2.75, u) * (1 - sm(2.9, 3.0, u)));
    ghostLine.color.copy(mint).lerp(amber, ordered); ghostFill.color.copy(ghostLine.color);
    ghost.scale.setScalar(1 + 0.03 * Math.sin(t * 3));
    crate.visible = solid > 0.01; crate.scale.setScalar(0.6 + 0.4 * solid);
    seal.scale.set(1, 0.001 + sm(3.86, 4.0, u), 1);
    coin.visible = paid > 0.01; coin.scale.setScalar(0.001 + paid); coin.rotation.y = t * 1.6;
    haloMat.color.copy(ghostLine.color).lerp(amber, paid);
    haloMat.opacity = 0.2 + 0.34 * Math.max(need, paid);
  } };
}

function buildEnvironment(renderer) {
  const env = new THREE.Scene();
  const lit = (hex, k) => new THREE.MeshBasicMaterial({ color: new THREE.Color(hex).multiplyScalar(k), side: THREE.DoubleSide });
  env.add(mesh(new THREE.BoxGeometry(40, 30, 40), new THREE.MeshBasicMaterial({ color: 0x0b1113, side: THREE.BackSide }), 0, 0, 0, false));
  const top = mesh(new THREE.PlaneGeometry(16, 16), lit(0xe6f6ee, 5), 0, 14, 0, false); top.rotation.x = Math.PI / 2;
  const warm = mesh(new THREE.PlaneGeometry(12, 9), lit(0xffd3a0, 3.4), 18, 4, 6, false); warm.rotation.y = -Math.PI / 2;
  const cool = mesh(new THREE.PlaneGeometry(14, 9), lit(0x9ee6ce, 2.4), -18, 3, -6, false); cool.rotation.y = Math.PI / 2;
  env.add(top, warm, cool);
  const pmrem = new THREE.PMREMGenerator(renderer);
  const target = pmrem.fromScene(env, 0.05);
  pmrem.dispose();
  env.traverse(o => { if (o.geometry) o.geometry.dispose(); if (o.material) o.material.dispose(); });
  return target;
}

/* =================================================================================================== */
export function createScene(canvas, options = {}) {
  const coarse = window.matchMedia('(pointer: coarse)').matches;
  const still = Boolean(options.still);            // movimiento reducido: sin bucle, un fotograma por cambio
  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true, powerPreference: 'high-performance' });
  renderer.setClearColor(0x000000, 0);
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.25;
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFShadowMap;
  let pixelRatio = Math.min(window.devicePixelRatio || 1, coarse ? 1.5 : 2);
  renderer.setPixelRatio(pixelRatio);

  const scene = new THREE.Scene();
  const envTarget = buildEnvironment(renderer);
  scene.environment = envTarget.texture;
  scene.environmentIntensity = 0.85;
  const camera = new THREE.PerspectiveCamera(30, 1, 0.5, 120);

  scene.add(new THREE.HemisphereLight(0xd8f3e8, 0x16262b, 0.9));
  const key = new THREE.DirectionalLight(0xfff1dc, 3.3);
  key.position.set(6, 11, 9);
  key.castShadow = true;
  key.shadow.mapSize.setScalar(coarse ? 1024 : 2048);
  Object.assign(key.shadow.camera, { left: -8.5, right: 8.5, top: 8.5, bottom: -8.5, near: 2, far: 34 });
  key.shadow.camera.updateProjectionMatrix();
  key.shadow.bias = -0.0004; key.shadow.normalBias = 0.03;
  scene.add(key);
  const rimLight = new THREE.DirectionalLight(C.mint, 1.6);
  rimLight.position.set(-9, 5, -7);
  scene.add(rimLight);
  const spot = new THREE.PointLight(C.mint, 0, 7.5, 1.6);
  scene.add(spot);

  const M = materials();
  const disposables = [];
  // Placa hexagonal, riel y radios hacia el núcleo.
  const slab = cyl(6.4, 6.66, 0.36, 6, M.plate, 0, -0.18, 0); slab.rotation.y = Math.PI / 6; scene.add(slab);
  const deck = cyl(6.22, 6.22, 0.04, 6, M.plateTop, 0, 0.02, 0); deck.rotation.y = Math.PI / 6; scene.add(deck);
  const edgePoints = Array.from({ length: 7 }, (_, i) => new THREE.Vector3(6.4 * Math.sin(i * STEP + Math.PI / 6), 0.005, 6.4 * Math.cos(i * STEP + Math.PI / 6)));
  scene.add(new THREE.Line(new THREE.BufferGeometry().setFromPoints(edgePoints), new THREE.LineBasicMaterial({ color: C.mint, transparent: true, opacity: 0.55 })));
  const track = mesh(new THREE.RingGeometry(R - 0.24, R + 0.24, 96), M.groove, 0, 0.045, 0, false);
  track.rotation.x = -Math.PI / 2; track.receiveShadow = true; scene.add(track);
  const railMat = M.glow(C.mint, 0.6);
  const rail = mesh(new THREE.TorusGeometry(R, 0.034, 8, 160), railMat, 0, 0.075, 0, false);
  rail.rotation.x = Math.PI / 2; scene.add(rail);
  const inner = mesh(new THREE.RingGeometry(2.16, 2.19, 96), new THREE.MeshBasicMaterial({ color: C.mint, transparent: true, opacity: 0.3 }), 0, 0.045, 0, false);
  inner.rotation.x = -Math.PI / 2; scene.add(inner);

  const core = buildCore(M);
  scene.add(core.group);
  const glowMap = glowTexture();
  const unit = buildUnit(M, glowMap);
  scene.add(unit.group);
  disposables.push(glowMap, unit.label.texture);

  const stations = BUILDERS.map((build, i) => {
    const a = stationAngle(i);
    const holder = new THREE.Group();
    holder.position.set(R * Math.cos(a), 0, R * Math.sin(a));
    holder.rotation.y = Math.atan2(Math.cos(a), Math.sin(a));       // +z local = hacia fuera
    scene.add(holder);
    holder.add(cyl(1.38, 1.46, 0.08, 56, M.pad, 0, 0.08, 0));
    const ringMat = M.glow(ACCENT[i], 0.18);
    const ring = mesh(new THREE.TorusGeometry(1.4, 0.016, 8, 72), ringMat, 0, 0.125, 0, false);
    ring.rotation.x = Math.PI / 2; holder.add(ring);
    const poolMat = new THREE.MeshBasicMaterial({ map: glowMap, color: ACCENT[i], transparent: true, opacity: 0, depthWrite: false, blending: THREE.AdditiveBlending });
    const pool = mesh(new THREE.PlaneGeometry(4.6, 4.6), poolMat, 0, 0.13, 0, false);
    pool.rotation.x = -Math.PI / 2; holder.add(pool);
    const label = floorLabel(i + 1, NAMES[i]);
    disposables.push(label.texture);
    const labelMesh = mesh(new THREE.PlaneGeometry(2.2, 0.55), new THREE.MeshBasicMaterial({ map: label.texture, transparent: true, depthWrite: false, depthTest: false }), 0, 0.06, 0, false);
    labelMesh.rotation.x = -Math.PI / 2; labelMesh.renderOrder = 5; scene.add(labelMesh);
    // Radio hacia el núcleo, con un paquete de datos que viaja cuando la estación trabaja.
    const spokeMat = M.glow(C.mint, 0.35);
    const spokeLength = R - 1.38 - 1.3;
    holder.add(box(0.05, 0.022, spokeLength, spokeMat, 0, 0.06, -1.38 - spokeLength / 2, false));
    const packetMat = M.glow(C.mint, 1.6); packetMat.transparent = true;
    const packet = box(0.12, 0.12, 0.12, packetMat, 0, 0.14, -1.38, false);
    holder.add(packet);
    const built = build(M);
    built.group.scale.setScalar(1.2);
    holder.add(built.group);
    const hit = mesh(new THREE.CylinderGeometry(1.6, 1.6, 2.6, 12), new THREE.MeshBasicMaterial({ transparent: true, opacity: 0, depthWrite: false }), 0, 1.3, 0, false);
    hit.userData.station = i; holder.add(hit);
    return { ...built, holder, ringMat, poolMat, spokeMat, packet, packetMat, spokeLength, hit, label, labelMesh, w: 0, clock: 0 };
  });
  // Los rótulos se repintan cuando la tipografía de la página termina de cargar.
  if (document.fonts && document.fonts.load) {
    document.fonts.load('600 80px "IBM Plex Sans Condensed"').then(() => {
      stations.forEach(st => st.label.paint()); unit.label.paint(); requestRender();
    }).catch(() => {});
  }

  /* ------------------------------------------------------------------ estado y encuadre */
  const target = { overview: 1, progress: 0 };
  const now = { overview: 1, progress: 0, fx: 0.5, fy: 0.5, u: 0, auto: 0, px: 0, py: 0, yaw: 0, t: 0, live: 0 };
  const frames = { hero: { x: 0.5, y: 0.5, span: 1 }, stage: { x: 0.5, y: 0.5, span: 0.6 } };
  const pointer = { x: 0, y: 0 };
  let hover = -1, width = 1, height = 1, aspect = 1, running = false, raf = 0, last = 0, disposed = false;
  const vA = new THREE.Vector3(), vB = new THREE.Vector3(), lookA = new THREE.Vector3(), lookB = new THREE.Vector3();
  const tint = new THREE.Color(), mintColor = new THREE.Color(C.mint), amberColor = new THREE.Color(C.amber);

  function placeCamera() {
    const b = sm(0, 1, now.overview);
    const dHero = clamp(1.452 / (frames.hero.span * aspect), 1.1, 3.8);
    const dStage = clamp(0.761 / (frames.stage.span * aspect), 0.95, 3);
    const a = stationAngle(now.progress);
    const camAngle = a + 0.3 + 0.1 * Math.sin(now.t * 0.45) * now.live;
    const reach = R + 5.2 * dStage;
    vA.set(Math.cos(camAngle) * reach, 3.6 * dStage, Math.sin(camAngle) * reach);
    lookA.set(Math.cos(a) * (R - 0.35), 0.85, Math.sin(a) * (R - 0.35));
    const turn = 0.5 * Math.sin(now.t * 0.2) * now.live;      // vaivén lento: la maqueta se ve desde varios ángulos
    vB.set(Math.sin(turn) * 12.7 * dHero, (10.6 + 0.5 * Math.sin(now.t * 0.31) * now.live) * dHero, Math.cos(turn) * 12.7 * dHero);
    lookB.set(0, 0.35, 0.5);
    camera.position.lerpVectors(vA, vB, b);
    lookA.lerp(lookB, b);
    // Paralaje leve con el puntero: la maqueta se siente física sin secuestrar el desplazamiento.
    camera.position.x += now.px * lerp(0.35, 1.1, b);
    camera.position.y += now.py * lerp(0.2, 0.6, b);
    camera.fov = lerp(34, 30, b);
    camera.lookAt(lookA);
    core.sign.rotation.y = Math.atan2(camera.position.x, camera.position.z);
    stations.forEach(st => {
      const dx = camera.position.x - st.holder.position.x, dz = camera.position.z - st.holder.position.z;
      const length = Math.hypot(dx, dz) || 1;
      st.labelMesh.position.set(st.holder.position.x + dx / length * 1.78, 0.06, st.holder.position.z + dz / length * 1.78);
      st.labelMesh.rotation.z = Math.atan2(dx, dz);
    });
    const fx = lerp(frames.stage.x, frames.hero.x, b), fy = lerp(frames.stage.y, frames.hero.y, b);
    now.fx = fx; now.fy = fy;
    camera.setViewOffset(width, height, (0.5 - fx) * width, (0.5 - fy) * height, width, height);
    camera.updateProjectionMatrix();
  }

  function step(dt, t) {
    const snap = dt === Infinity;
    now.t = t; now.live = snap ? 0 : 1;
    const d = (cur, to, lambda) => (snap ? to : damp(cur, to, lambda, dt));
    now.overview = d(now.overview, target.overview, 3.4);
    now.progress = d(now.progress, target.progress, 3.2);
    now.px = d(now.px, pointer.x, 2.4); now.py = d(now.py, pointer.y, 2.4);
    if (target.overview > 0.5 && !snap) {
      now.auto = (now.auto + dt * 0.3) % 6;                // la unidad recorre sola el circuito en la portada
      now.u = now.auto;
    } else {
      const goal = snap && target.overview > 0.5 ? 0.5 : target.progress;
      let delta = goal - now.u;
      if (delta > 3) delta -= 6; else if (delta < -3) delta += 6;
      now.u = snap ? goal : (now.u + delta * (1 - Math.exp(-2.8 * dt)) + 6) % 6;
      now.auto = now.u;
    }
    const along = railParam(now.u);
    const ua = stationAngle(along);
    const bob = snap ? 0 : 0.035 * Math.sin(t * 2.2);
    unit.group.position.set(R * Math.cos(ua), 0.66 + bob, R * Math.sin(ua));
    unit.group.rotation.y = Math.atan2(Math.cos(ua), Math.sin(ua));
    unit.group.scale.setScalar(1.3 + 0.45 * sm(0, 1, now.overview));
    unit.update(along, t);

    let strongest = 0, amberMix = 0;
    stations.forEach((st, i) => {
      const goal = 1 - sm(0.22, 0.72, cyclic(along, i));
      st.w = d(st.w, goal, 4.2);
      const w = st.w, lit = Math.max(w, hover === i ? 0.55 : 0);
      st.clock = !snap && w > 0.9 ? st.clock + dt : 0;
      st.update(st.clock > 0 ? replay(st.clock) : w, t);
      st.ringMat.emissiveIntensity = 0.18 + 0.75 * lit;
      st.poolMat.opacity = 0.16 * lit;
      st.spokeMat.emissiveIntensity = 0.35 + 1.2 * w;
      const f = snap ? 0.5 : (t * 0.55 + i * 0.17) % 1;
      st.packet.position.z = -1.38 - f * st.spokeLength;
      st.packetMat.opacity = w * Math.sin(f * Math.PI);
      st.packet.visible = w > 0.03;
      if (w > strongest) strongest = w;
      if (ACCENT[i] === C.amber) amberMix = Math.max(amberMix, w);
    });
    core.tileMat.emissiveIntensity = 0.3 + 0.4 * strongest + (snap ? 0 : 0.06 * Math.sin(t * 1.7));
    tint.copy(mintColor).lerp(amberColor, amberMix);
    spot.color.copy(tint);
    spot.intensity = 8 + 18 * strongest;
    spot.position.set((R + 0.5) * Math.cos(ua), 2.4, (R + 0.5) * Math.sin(ua));
    placeCamera();
  }

  /* ------------------------------------------------------------------ bucle, calidad y tamaño */
  let slow = 0, frameCount = 0;
  function govern(dt) {
    // Si el equipo no sostiene el ritmo, baja primero la densidad de píxeles y luego las sombras.
    frameCount += 1;
    if (frameCount < 30) return;
    slow = dt > 0.034 ? slow + 1 : Math.max(0, slow - 1);
    if (slow < 45) return;
    slow = 0;
    if (pixelRatio > 1) { pixelRatio = Math.max(1, pixelRatio - 0.5); renderer.setPixelRatio(pixelRatio); renderer.setSize(width, height, false); }
    else if (renderer.shadowMap.enabled) { renderer.shadowMap.enabled = false; key.castShadow = false; scene.traverse(o => { if (o.material) o.material.needsUpdate = true; }); }
  }
  function draw() { renderer.render(scene, camera); }
  function loop(time) {
    if (!running) return;
    raf = requestAnimationFrame(loop);
    const dt = Math.min(0.05, (time - last) / 1000 || 0.016);
    last = time;
    step(dt, time / 1000);
    draw();
    govern(dt);
  }
  function requestRender() {
    if (disposed || running) return;
    step(Infinity, 0); draw();
  }
  function start() { if (still || running || disposed) return; running = true; last = performance.now(); raf = requestAnimationFrame(loop); }
  function stop() { running = false; cancelAnimationFrame(raf); }
  function resize() {
    const rect = canvas.getBoundingClientRect();
    width = Math.max(1, Math.round(rect.width)); height = Math.max(1, Math.round(rect.height));
    aspect = width / height;
    renderer.setSize(width, height, false);
    camera.aspect = aspect;
    if (!running) requestRender();
  }

  const raycaster = new THREE.Raycaster();
  const ndc = new THREE.Vector2();
  const hits = stations.map(st => st.hit);
  function pick(clientX, clientY) {
    const rect = canvas.getBoundingClientRect();
    if (clientX < rect.left || clientX > rect.right || clientY < rect.top || clientY > rect.bottom) return -1;
    ndc.set(((clientX - rect.left) / rect.width) * 2 - 1, -((clientY - rect.top) / rect.height) * 2 + 1);
    raycaster.setFromCamera(ndc, camera);
    const found = raycaster.intersectObjects(hits, false)[0];
    return found ? found.object.userData.station : -1;
  }

  canvas.addEventListener('webglcontextlost', event => { event.preventDefault(); stop(); if (options.onLost) options.onLost(); });
  resize();
  step(Infinity, 0);
  draw();

  return {
    set(next) {
      if (next.hero) Object.assign(frames.hero, next.hero);
      if (next.overview !== undefined) target.overview = next.overview;
      if (next.progress !== undefined) target.progress = next.progress;
      if (!running) requestRender();
    },
    setStageFrame(frame) { Object.assign(frames.stage, frame); if (!running) requestRender(); },
    setPointer(x, y) { if (still) return; pointer.x = x; pointer.y = y; },
    setHover(i) { if (hover === i) return; hover = i; if (!running) requestRender(); },
    pick, resize, start, stop,
    snapshot(type = 'image/webp', quality = 0.9) { step(Infinity, 0); draw(); return canvas.toDataURL(type, quality); },
    dispose() {
      disposed = true; stop();
      scene.traverse(o => {
        if (o.geometry) o.geometry.dispose();
        if (o.material) (Array.isArray(o.material) ? o.material : [o.material]).forEach(m => m.dispose());
      });
      disposables.forEach(t => t.dispose());
      envTarget.dispose(); renderer.dispose();
    },
  };
}
