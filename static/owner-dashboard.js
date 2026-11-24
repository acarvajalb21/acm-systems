// Tablero de la dirección: detalle al pasar el cursor, comparación anual, pestañas y filtros por antigüedad.
// Sin este archivo todo sigue visible: tablas, documentos y valores también están en el HTML.
(function () {
  'use strict';
  var $$ = function (sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); };

  // 1. Un solo cuadro de detalle para todos los gráficos (valores con textContent, nunca HTML).
  var tip = document.createElement('div');
  tip.className = 'chart-tip';
  tip.setAttribute('aria-hidden', 'true');
  tip.hidden = true;
  document.body.appendChild(tip);
  var owner = null;

  function render(data) {
    tip.textContent = '';
    var title = document.createElement('strong');
    title.className = 'chart-tip-title';
    title.textContent = data.title || '';
    tip.appendChild(title);
    (data.rows || []).forEach(function (row) {
      var line = document.createElement('div');
      line.className = 'chart-tip-row';
      var key = document.createElement('span');
      key.className = 'chart-tip-key' + (row[2] ? ' key-' + row[2] : '');
      var label = document.createElement('span');
      label.textContent = row[0];
      var value = document.createElement('b');
      value.textContent = row[1];
      line.appendChild(key); line.appendChild(label); line.appendChild(value);
      tip.appendChild(line);
    });
    if (data.hint) {
      var hint = document.createElement('small');
      hint.textContent = data.hint;
      tip.appendChild(hint);
    }
  }
  function place(x, y) {
    var w = tip.offsetWidth, h = tip.offsetHeight;
    var left = x + 16, top = y + 16;
    if (left + w > window.innerWidth - 8) left = x - w - 16;
    if (top + h > window.innerHeight - 8) top = y - h - 16;
    tip.style.left = Math.max(8, left) + 'px';
    tip.style.top = Math.max(8, top) + 'px';
  }
  function show(el, x, y) {
    var data;
    try { data = JSON.parse(el.getAttribute('data-tip')); } catch (e) { return; }
    if (owner && owner !== el) owner.classList.remove('is-hover');
    owner = el;
    render(data);
    tip.hidden = false;
    el.classList.add('is-hover');
    if (x === undefined) {
      var box = el.getBoundingClientRect();
      x = box.left + Math.min(box.width / 2, 120);
      y = box.bottom - 4;
    }
    place(x, y);
  }
  function hide(el) {
    if (el && el !== owner) return;
    tip.hidden = true;
    if (owner) owner.classList.remove('is-hover');
    owner = null;
  }
  $$('[data-tip]').forEach(function (el) {
    $$(':scope > title', el).forEach(function (t) { t.remove(); });
    if (!el.hasAttribute('data-tip-focus')) {
      el.addEventListener('pointerenter', function (e) { show(el, e.clientX, e.clientY); });
      el.addEventListener('pointermove', function (e) { if (owner === el) place(e.clientX, e.clientY); });
      el.addEventListener('pointerleave', function () { hide(el); });
    }
    el.addEventListener('focus', function () { if (el.matches(':focus-visible')) show(el); });
    el.addEventListener('blur', function () { hide(el); });
  });
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') hide(); });
  window.addEventListener('scroll', function () { if (owner && !owner.matches(':focus')) hide(); }, { passive: true });

  // 2. Comparar con el año anterior: alterna la geometría (barras solas o en pareja) y su escala.
  $$('[data-compare]').forEach(function (button) {
    var chart = document.getElementById(button.getAttribute('aria-controls'));
    var card = button.closest('.card');
    button.addEventListener('click', function () {
      var on = button.getAttribute('aria-pressed') !== 'true';
      button.setAttribute('aria-pressed', String(on));
      if (chart) chart.classList.toggle('is-compare', on);
      if (card) card.classList.toggle('is-compare', on);
    });
  });

  // 3. Pestañas «Nos deben» / «Debemos» (sin JavaScript se ven ambas, una debajo de otra).
  var tabs = $$('[data-debt-tab]');
  function select(tab, focus) {
    tabs.forEach(function (t) {
      var on = t === tab;
      t.setAttribute('aria-selected', String(on));
      t.tabIndex = on ? 0 : -1;
      var panel = document.getElementById(t.getAttribute('aria-controls'));
      if (panel) panel.hidden = !on;
    });
    if (focus) tab.focus();
  }
  if (tabs.length) {
    var box = document.querySelector('[data-debts]');
    if (box) box.classList.add('is-enhanced');
    tabs.forEach(function (tab, i) {
      tab.addEventListener('click', function () { select(tab); });
      tab.addEventListener('keydown', function (e) {
        if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
        e.preventDefault();
        select(tabs[(i + (e.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length], true);
      });
    });
    select(tabs[0]);
  }

  // 4. Filtro por antigüedad: deja solo los terceros y documentos del tramo elegido.
  $$('[data-panel]').forEach(function (panel) {
    var buttons = $$('[data-bucket]', panel);
    var status = panel.querySelector('[data-filter-status]');
    function apply(key) {
      buttons.forEach(function (b) { b.setAttribute('aria-pressed', String(b.getAttribute('data-bucket') === key)); });
      panel.classList.toggle('is-filtered', key !== 'all');
      var shown = 0;
      $$('.debt-entity', panel).forEach(function (row) {
        var has = key === 'all' || (' ' + row.getAttribute('data-buckets') + ' ').indexOf(' ' + key + ' ') !== -1;
        row.hidden = !has;
        if (has) shown += 1;
        if (key !== 'all' && has && !row.open) row.open = shown <= 3;
      });
      $$('[data-doc-bucket]', panel).forEach(function (tr) { tr.hidden = key !== 'all' && tr.getAttribute('data-doc-bucket') !== key; });
      $$('.seg, .sum-seg', panel).forEach(function (seg) { seg.classList.toggle('is-dim', key !== 'all' && !seg.classList.contains('age-' + key)); });
      if (status) {
        var active = buttons.filter(function (b) { return b.getAttribute('data-bucket') === key; })[0];
        status.textContent = key === 'all' ? '' : 'Mostrando ' + active.childNodes[1].textContent.trim().toLowerCase() + ': ' + shown + (shown === 1 ? ' tercero.' : ' terceros.');
      }
    }
    buttons.forEach(function (b) {
      b.addEventListener('click', function () {
        var key = b.getAttribute('data-bucket');
        apply(b.getAttribute('aria-pressed') === 'true' && key !== 'all' ? 'all' : key);
      });
    });
    $$('.sum-seg', panel).forEach(function (seg) {
      seg.addEventListener('click', function () { apply(seg.getAttribute('data-bucket-key')); });
    });
  });
})();
