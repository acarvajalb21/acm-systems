/* Interacciones de la presentación. Solo estado de esta página: no escribe ni consulta datos de la demostración.
   La maqueta 3D (escena3d.js) se carga después del primer pintado; si WebGL no está disponible, o la dirección
   lleva ?3d=0, la página conserva el póster y el selector de estaciones con sus dibujos. */
(() => {
  'use strict';
  const byId = id => document.getElementById(id);
  const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
  const reduced = window.matchMedia('(prefers-reduced-motion: reduce)');
  const wide = window.matchMedia('(min-width: 961px)');

  /* ------------------------------------------------------------ selectores de vistas y estaciones */
  function switchPanel(button, buttons, panels, prefix, attribute, animate) {
    buttons.forEach(item => item.setAttribute('aria-pressed', String(item === button)));
    const target = byId(prefix + button.dataset[attribute]);
    panels.forEach(panel => { panel.hidden = panel !== target; panel.classList.remove('acm-switched'); });
    if (animate && !reduced.matches) {
      target.classList.add('acm-switched');
      target.addEventListener('animationend', () => target.classList.remove('acm-switched'), { once: true });
    }
  }
  const previewButtons = [...document.querySelectorAll('[data-preview]')];
  const previewPanels = [...document.querySelectorAll('.acm-preview-panel')];
  previewButtons.forEach(button => button.addEventListener('click', event => {
    switchPanel(button, previewButtons, previewPanels, 'preview-', 'preview', event.detail > 0);
  }));

  /* ------------------------------------------------------------------------------- el recorrido */
  const stage = byId('escenario');
  const journey = byId('funciona');
  const canvas = stage.querySelector('.acm-stage-canvas');
  const poster = stage.querySelector('.acm-poster');
  const flowButtons = [...document.querySelectorAll('[data-step]')];
  const flowPanels = [...document.querySelectorAll('.acm-flow-panel')];
  const flowProgress = byId('flow-progress');
  const play = byId('flow-play');
  const playText = byId('flow-play-text');
  const LAST = flowButtons.length - 1;
  let scene = null, pinned = false, current = 0, ticking = false, playing = false, playTimer = 0;

  function showStage(index, animate) {
    if (index === current) return;
    current = index;
    switchPanel(flowButtons[index], flowButtons, flowPanels, 'flow-', 'step', animate);
    flowProgress.style.transform = `scaleX(${(index + 1) / flowButtons.length})`;
  }
  function metrics() {
    const top = journey.getBoundingClientRect().top + window.scrollY;
    const vh = window.innerHeight;
    return { top, vh, span: Math.max(1, journey.offsetHeight - vh) };
  }
  function goTo(index, animate = true) {
    if (pinned) {
      const m = metrics();
      window.scrollTo({ top: m.top + m.span * (index + 0.5) / flowButtons.length, behavior: reduced.matches ? 'instant' : 'smooth' });
    } else {
      showStage(index, animate);
      sync();
    }
  }
  /* Encuadre: en la portada la maqueta ocupa el lugar exacto del póster; en el recorrido, un punto fijo. */
  function sync() {
    ticking = false;
    if (!scene) return;
    const m = metrics();
    const y = window.scrollY;
    let overview, progress;
    if (pinned) {
      progress = clamp(clamp((y - m.top) / m.span, 0, 1) * flowButtons.length - 0.5, 0, LAST);
      const t = clamp((y - (m.top - m.vh * 0.92)) / (m.vh * 0.8), 0, 1);
      overview = 1 - t * t * (3 - 2 * t);
      showStage(Math.round(progress), true);
    } else {
      progress = current;
      overview = y > m.top - m.vh * 0.45 ? 0 : 1;
    }
    const box = poster.getBoundingClientRect();
    const view = canvas.getBoundingClientRect();
    scene.set({ overview, progress, hero: {
      x: (box.left + box.width / 2 - view.left) / view.width,
      y: (box.top + box.height / 2 - view.top) / view.height,
      span: 0.8 * box.width / view.width,
    } });
  }
  function requestSync() { if (!ticking) { ticking = true; requestAnimationFrame(sync); } }
  function layoutScene() {
    if (!scene) return;
    scene.setStageFrame(wide.matches ? { x: 0.69, y: 0.47, span: 0.27 } : { x: 0.5, y: 0.33, span: 0.7 });
    scene.resize();
    sync();
  }

  flowButtons.forEach((button, index) => button.addEventListener('click', event => { stopPlay(false); goTo(index, event.detail > 0); }));
  // Las flechas complementan Tab/Enter sin crear un control dependiente del ratón.
  [previewButtons, flowButtons].forEach(buttons => buttons.forEach((button, index) => {
    button.addEventListener('keydown', event => {
      if (!['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault(); event.stopPropagation();
      const step = event.key === 'ArrowRight' || event.key === 'ArrowDown' ? 1 : -1;
      const next = event.key === 'Home' ? 0 : event.key === 'End' ? buttons.length - 1 : (index + step + buttons.length) % buttons.length;
      buttons[next].focus({ preventScroll: true }); buttons[next].click();
    });
  }));

  /* Secuencia guiada: avanza de estación en estación; cualquier gesto de desplazamiento la detiene. */
  function stopPlay(done) {
    if (!playing && !done) return;
    playing = false; clearTimeout(playTimer);
    play.classList.remove('is-playing');
    playText.textContent = done ? 'Repetir recorrido' : 'Reproducir recorrido';
  }
  function startPlay() {
    playing = true;
    play.classList.add('is-playing');
    playText.textContent = 'Pausar recorrido';
    let index = current >= LAST ? 0 : current;
    goTo(index);
    const next = () => {
      if (!playing) return;
      if (index >= LAST) { stopPlay(true); return; }
      index += 1; goTo(index);
      playTimer = setTimeout(next, 3600);
    };
    playTimer = setTimeout(next, 3200);
  }
  play.addEventListener('click', () => (playing ? stopPlay(false) : startPlay()));
  window.addEventListener('wheel', () => stopPlay(false), { passive: true });
  window.addEventListener('touchmove', () => stopPlay(false), { passive: true });
  window.addEventListener('keydown', event => {
    if (['ArrowUp', 'ArrowDown', 'PageUp', 'PageDown', 'Home', 'End', ' '].includes(event.key) && event.target !== play) stopPlay(false);
  });

  /* --------------------------------------------------------------------------- maqueta 3D diferida */
  function supportsWebGL() {
    try {
      const gl = document.createElement('canvas').getContext('webgl2');
      if (!gl) return false;
      const lose = gl.getExtension('WEBGL_lose_context');
      if (lose) lose.loseContext();
      return true;
    } catch (error) { return false; }
  }
  function fallback() {
    if (scene) { scene.dispose(); scene = null; }
    stopPlay(false);
    pinned = false; play.hidden = true;
    stage.classList.remove('is-3d', 'is-pinned', 'is-ready', 'can-pick');
  }
  async function boot() {
    if (new URLSearchParams(window.location.search).get('3d') === '0' || !supportsWebGL()) return;
    try {
      const module = await import(stage.dataset.scene);
      stage.classList.add('is-3d');
      pinned = !reduced.matches;
      stage.classList.toggle('is-pinned', pinned);
      scene = module.createScene(canvas, { still: reduced.matches, onLost: fallback });
      play.hidden = false;
      layoutScene();
      if (!document.hidden) scene.start();
      requestAnimationFrame(() => stage.classList.add('is-ready'));
    } catch (error) {
      fallback();
    }
  }
  if (document.readyState === 'complete') boot();
  else window.addEventListener('load', () => (window.requestIdleCallback ? window.requestIdleCallback(boot, { timeout: 900 }) : setTimeout(boot, 120)), { once: true });

  window.addEventListener('scroll', requestSync, { passive: true });
  window.addEventListener('resize', () => requestAnimationFrame(layoutScene));
  wide.addEventListener('change', layoutScene);
  document.addEventListener('visibilitychange', () => { if (scene) (document.hidden ? scene.stop() : stageVisible && scene.start()); });
  let stageVisible = true;
  if ('IntersectionObserver' in window) {
    new IntersectionObserver(entries => {
      stageVisible = entries[0].isIntersecting;
      if (scene) (stageVisible && !document.hidden ? scene.start() : scene.stop());
    }).observe(stage);
  }
  // El lienzo queda detrás del contenido: el escenario recibe el puntero y pregunta a la maqueta qué estación hay debajo.
  let pointerEvent = null;
  function readPointer() {
    const event = pointerEvent; pointerEvent = null;
    if (!scene || !event) return;
    const blocked = event.target.closest('a, button, .acm-flow-panel, .acm-hero-copy');
    const index = blocked ? -1 : scene.pick(event.clientX, event.clientY);
    scene.setHover(index);
    stage.classList.toggle('can-pick', index >= 0);
    scene.setPointer((event.clientX / window.innerWidth - 0.5) * 1.2, (0.5 - event.clientY / window.innerHeight) * 1.2);
  }
  stage.addEventListener('pointermove', event => {
    if (event.pointerType === 'touch') return;
    if (!pointerEvent) requestAnimationFrame(readPointer);
    pointerEvent = event;
  });
  stage.addEventListener('pointerleave', () => { if (scene) { scene.setHover(-1); scene.setPointer(0, 0); } stage.classList.remove('can-pick'); });
  stage.addEventListener('click', event => {
    if (!scene || event.target.closest('a, button, .acm-flow-panel')) return;
    const index = scene.pick(event.clientX, event.clientY);
    if (index < 0) return;
    stopPlay(false);
    if (!pinned) journey.scrollIntoView({ behavior: 'instant' });
    goTo(index);
  });

  /* ----------------------------------------------------------------- inventario: despacho por lotes */
  const lots = [...document.querySelectorAll('#lots li')];
  const lotBase = lots.map(item => Number(item.dataset.lot));
  const lotMax = Math.max(...lotBase);
  const lotDispatch = byId('lot-dispatch');
  const lotReset = byId('lot-reset');
  function renderLots(values) {
    lots.forEach((item, i) => {
      item.querySelector('b span').textContent = String(values[i]);
      item.querySelector('.acm-lot-bar i').style.setProperty('--fill', String(values[i] / lotMax));
      item.classList.toggle('is-empty', values[i] === 0);
    });
    byId('lot-total').textContent = String(values.reduce((a, b) => a + b, 0));
  }
  renderLots(lotBase);
  lotDispatch.addEventListener('click', () => {
    let left = 60;
    renderLots(lotBase.map(quantity => { const take = Math.min(quantity, left); left -= take; return quantity - take; }));
    byId('lot-note').textContent = 'Salieron 48 ud. del lote SL-041, que vence primero, y 12 ud. del SL-044.';
    lotDispatch.disabled = true; lotReset.hidden = false;
    lotReset.focus({ preventScroll: true });
  });
  lotReset.addEventListener('click', () => {
    renderLots(lotBase);
    byId('lot-note').textContent = 'El lote que vence primero, sale primero.';
    lotDispatch.disabled = false; lotReset.hidden = true;
    lotDispatch.focus({ preventScroll: true });
  });

  /* --------------------------------------- agente: situación → motivo → revisión humana → resultado */
  const agentSeq = byId('agent-seq');
  const agentStatus = byId('agent-status');
  const agentReset = byId('agent-reset');
  const AGENT = {
    review: ['Pendiente de revisión', 'acm-chip acm-chip-amber', 'Propuesta: reponer 36 unidades. Falta tu decisión.', 'Aparece cuando decides. Esta prueba no crea pedidos en la demo.'],
    approved: ['Aprobada · ejemplo', 'acm-chip', 'Aprobaste la propuesta de ejemplo.', 'Pedido de ejemplo preparado: 36 unidades de sellante industrial. En la aplicación, esta decisión queda registrada. Aquí no se ha creado ningún pedido.'],
    rejected: ['Rechazada · ejemplo', 'acm-chip acm-chip-coral', 'Rechazaste la propuesta de ejemplo.', 'Propuesta descartada: el agente no ejecuta nada sin aprobación. Aquí no cambió ningún registro.'],
  };
  function setAgent(state) {
    const [label, chip, review, message] = AGENT[state];
    agentSeq.dataset.state = state;
    agentStatus.textContent = label; agentStatus.className = chip;
    byId('agent-review').textContent = review;
    byId('agent-message').textContent = message;
    agentReset.hidden = state === 'review';
    (state === 'review' ? byId('agent-approve') : agentReset).focus({ preventScroll: true });
  }
  byId('agent-approve').addEventListener('click', () => setAgent('approved'));
  byId('agent-reject').addEventListener('click', () => setAgent('rejected'));
  agentReset.addEventListener('click', () => setAgent('review'));

  /* ------------------------------------------------------------- laboratorio de abastecimiento */
  const STOCK = 120, CRATE = 20, WEEKS = 8;
  const demand = byId('demand');
  const lead = byId('lead-time');
  const crates = [...byId('crates').children];
  const weeksBar = byId('weeks');
  const number = new Intl.NumberFormat('es-CO', { maximumFractionDigits: 1 });
  crates.forEach((crate, i) => crate.style.setProperty('--i', String(i)));
  function calculate() {
    const units = Number(demand.value);
    const weeks = Number(lead.value);
    const need = units * (weeks + 1);
    const order = Math.max(0, need - STOCK);
    const coverage = STOCK / units;
    const weekWord = n => (n === 1 ? 'semana' : 'semanas');
    byId('demand-output').textContent = `${units} ud.`;
    byId('lead-output').textContent = `${weeks} ${weekWord(weeks)}`;
    demand.setAttribute('aria-valuetext', `${units} unidades por semana`);
    lead.setAttribute('aria-valuetext', `${weeks} ${weekWord(weeks)}`);
    byId('need-units').textContent = String(need);
    byId('order-units').textContent = String(order);
    const stockCrates = STOCK / CRATE, needCrates = Math.ceil(need / CRATE);
    crates.forEach((crate, i) => {
      const state = i < Math.min(stockCrates, needCrates) ? 'stock' : i < stockCrates ? 'spare' : i < needCrates ? 'order' : '';
      if (crate.dataset.s !== state) crate.dataset.s = state;
    });
    byId('crates').setAttribute('aria-label', `Stock: ${STOCK} unidades. Necesidad: ${need} unidades. Por reponer: ${order} unidades.`);
    weeksBar.style.setProperty('--cover', String(Math.min(coverage, WEEKS) / WEEKS));
    weeksBar.style.setProperty('--lead', String(weeks));
    weeksBar.dataset.short = String(coverage < weeks);
    weeksBar.dataset.edge = String(weeks >= 6);
    weeksBar.setAttribute('aria-label', `El stock cubre ${number.format(coverage)} ${weekWord(coverage)}; el pedido llega en ${weeks} ${weekWord(weeks)}.`);
    const status = byId('stock-status');
    status.textContent = `Tu stock cubre ${number.format(coverage)} ${weekWord(coverage)}. ` +
      (order === 0 ? 'Tienes cubiertos el plazo y la reserva.' : coverage < weeks ? 'No alcanza hasta la entrega. Revisa el plazo con tu proveedor.' : 'Repón antes de consumir la reserva.');
    status.parentElement.dataset.shortage = String(coverage < weeks);
    status.parentElement.dataset.order = order === 0 ? '0' : '1';
  }
  demand.addEventListener('input', calculate);
  lead.addEventListener('input', calculate);
  byId('sim-reset').addEventListener('click', () => { demand.value = '40'; lead.value = '3'; calculate(); });
  calculate();

  /* --------------------------------------------------------------------------------- menú móvil */
  const menu = document.querySelector('.acm-menu');
  const navigation = byId('acm-navigation');
  function closeMenu(returnFocus = false) {
    menu.setAttribute('aria-expanded', 'false'); menu.setAttribute('aria-label', 'Abrir menú');
    navigation.classList.remove('is-open');
    if (returnFocus) menu.focus();
  }
  menu.addEventListener('click', () => {
    const open = menu.getAttribute('aria-expanded') !== 'true';
    menu.setAttribute('aria-expanded', String(open));
    menu.setAttribute('aria-label', open ? 'Cerrar menú' : 'Abrir menú');
    navigation.classList.toggle('is-open', open);
  });
  navigation.querySelectorAll('a').forEach(link => link.addEventListener('click', () => {
    const isMobile = getComputedStyle(menu).display !== 'none';
    closeMenu();
    if (isMobile) {
      const target = document.querySelector(link.hash);
      target.setAttribute('tabindex', '-1'); target.focus({ preventScroll: true });
    }
  }));
  document.addEventListener('keydown', event => { if (event.key === 'Escape' && menu.getAttribute('aria-expanded') === 'true') closeMenu(true); });
  document.addEventListener('click', event => { if (!event.target.closest('.acm-header')) closeMenu(); });
  wide.addEventListener('change', () => closeMenu());
  if ('IntersectionObserver' in window) {
    const sectionObserver = new IntersectionObserver(entries => {
      entries.forEach(entry => {
        if (!entry.isIntersecting) return;
        navigation.querySelectorAll('a').forEach(link => {
          if (link.hash === '#' + entry.target.id) link.setAttribute('aria-current', 'location');
          else link.removeAttribute('aria-current');
        });
      });
    }, { rootMargin: '-15% 0px -55% 0px' });
    document.querySelectorAll('#funciona,#incluye,#simulador,#equipo,#inicio').forEach(section => sectionObserver.observe(section));
  }
})();
