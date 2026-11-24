// Interacciones de interfaz. Las reglas de negocio se validan siempre en el servidor.
(function () {
  'use strict';
  var $ = function (sel, root) { return (root || document).querySelector(sel); };
  var $$ = function (sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); };
  var num = function (v) { var n = parseFloat(String(v || '').replace(',', '.')); return isFinite(n) ? n : 0; };
  var cop = function (n) { return (n < 0 ? '−$' : '$') + Math.abs(Math.round(n)).toLocaleString('es-CO'); };

  // App instalable: el service worker solo ofrece la página «sin conexión». Requiere contexto seguro (https o localhost).
  var swUrl = document.body && document.body.getAttribute('data-sw');
  if (swUrl && 'serviceWorker' in navigator && window.isSecureContext) {
    window.addEventListener('load', function () { navigator.serviceWorker.register(swUrl, { scope: '/' }).catch(function () { /* sin app instalable */ }); });
  }

  // Hoja «Más» del celular: estado accesible, foco contenido y retorno al botón que la abrió.
  var sheet = $('#mas-opciones'), opener = null;
  var compact = window.matchMedia('(max-width: 760px)');
  function sheetOpen() { return document.body.classList.contains('nav-open'); }
  function setSheet(open) {
    if (!sheet) return;
    open = Boolean(open) && compact.matches;
    document.body.classList.toggle('nav-open', open);
    sheet.inert = !open;
    $$('.topbar, .subnav, .main').forEach(function (el) { el.inert = open; });
    $$('[data-nav-toggle]').forEach(function (button) { button.setAttribute('aria-expanded', String(open)); });
    if (open) {
      opener = document.activeElement;
      var first = $('button[data-nav-close]', sheet);
      if (first) first.focus({ preventScroll: true });
    } else if (opener) {
      if (compact.matches && opener.focus) opener.focus({ preventScroll: true });
      opener = null;
    }
  }
  if (sheet) {
    sheet.inert = true;
    $$('[data-nav-toggle]').forEach(function (button) { button.addEventListener('click', function () { setSheet(!sheetOpen()); }); });
    $$('[data-nav-close]').forEach(function (el) { el.addEventListener('click', function () { setSheet(false); }); });
    compact.addEventListener('change', function () { setSheet(false); });
  }

  // La página activa queda a la vista en la segunda fila cuando esta se desplaza (celular).
  var subnav = $('.subnav-row'), currentPage = subnav && $('[aria-current]', subnav);
  if (currentPage && subnav.scrollWidth > subnav.clientWidth) subnav.scrollLeft = currentPage.offsetLeft - (subnav.clientWidth - currentPage.offsetWidth) / 2;

  // Menús desplegables (persona y confirmaciones): Escape los cierra y devuelve el foco; un clic fuera también.
  var POPUPS = 'details.user-menu[open], details.confirm[open]';
  document.addEventListener('keydown', function (event) {
    if (event.key === 'Escape') {
      if (sheetOpen()) { setSheet(false); return; }
      $$(POPUPS).forEach(function (menu) {
        var summary = $('summary', menu);
        if (menu.contains(document.activeElement) && summary) summary.focus();
        menu.open = false;
      });
      return;
    }
    if (event.key !== 'Tab' || !sheetOpen()) return;
    var focusable = $$('a[href], button:not([disabled])', sheet);
    var first = focusable[0], last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  });
  document.addEventListener('click', function (e) {
    $$(POPUPS).forEach(function (d) { if (!d.contains(e.target)) d.open = false; });
  });

  // Mensajes descartables.
  $$('.message [data-dismiss]').forEach(function (b) {
    b.addEventListener('click', function () { b.closest('.message').remove(); });
  });

  // Confirmación de acciones sensibles y bloqueo de doble envío.
  $$('form').forEach(function (f) {
    f.addEventListener('submit', function (e) {
      if (e.defaultPrevented || f.hasAttribute('data-fetch')) return;   // los formularios del escáner manejan su propio estado
      var msg = f.getAttribute('data-confirm');
      if (msg && !window.confirm(msg)) { e.preventDefault(); return; }
      if (f.dataset.sent) { e.preventDefault(); return; }
      f.dataset.sent = '1';
      $$('button[type=submit]', f).forEach(function (b) { b.setAttribute('aria-busy', 'true'); });
      window.setTimeout(function () {
        delete f.dataset.sent;
        $$('button[type=submit]', f).forEach(function (b) { b.removeAttribute('aria-busy'); });
      }, 8000);
    });
  });
  // Al volver con el botón Atrás desde la memoria del navegador, el formulario queda disponible otra vez.
  window.addEventListener('pageshow', function (e) {
    if (!e.persisted) return;
    $$('form[data-sent]').forEach(function (f) { delete f.dataset.sent; });
    $$('button[aria-busy]').forEach(function (b) { b.removeAttribute('aria-busy'); });
  });

  // Filtros que se aplican al elegir una opción (el botón «Filtrar» sigue disponible).
  $$('select[data-autosubmit]').forEach(function (select) {
    select.addEventListener('change', function () {
      if (select.form && select.form.requestSubmit) select.form.requestSubmit();
    });
  });

  // Tablas de detalle → registros apilados en celular. Las que ya traen `mobile-records` y `data-label` desde la plantilla
  // no se tocan; a las demás que tienen encabezado se les copia aquí el rótulo de cada columna. Sin este archivo conservan
  // el desplazamiento dentro de su contenedor, así que nunca desbordan la página. Las tablas de captura (`lines`) y las
  // marcadas con `data-no-stack` quedan como están.
  $$('.table-wrap > table').forEach(function (table) {
    if (table.classList.contains('mobile-records') || table.classList.contains('lines') || table.hasAttribute('data-no-stack')) return;
    var head = $$('thead tr', table);
    if (head.length !== 1) return;
    var labels = $$('th, td', head[0]).map(function (th) {
      var hidden = $('.sr-only', th);
      return hidden && hidden.textContent.trim() === th.textContent.trim() ? '' : th.textContent.replace(/\s+/g, ' ').trim();
    });
    if (labels.length < 2) return;
    $$('tbody > tr, tfoot > tr', table).forEach(function (tr) {
      var cells = Array.prototype.slice.call(tr.children);
      if (cells.length !== labels.length) return;                      // filas con celdas combinadas: ocupan todo el ancho
      cells.forEach(function (cell, i) {
        if (cell.hasAttribute('colspan')) return;
        if (i === 0) cell.classList.add('record-title');
        else if (labels[i]) cell.setAttribute('data-label', labels[i]);
        else cell.classList.add('record-actions');
        if (i > 0 && ($('ul, ol, p, form', cell) || cell.textContent.replace(/\s+/g, ' ').trim().length > 70)) cell.classList.add('record-wide');
      });
    });
    table.classList.add('mobile-records');
  });

  // Filtro rápido de tablas.
  $$('[data-filter]').forEach(function (input) {
    var table = $(input.getAttribute('data-filter'));
    if (!table) return;
    var empty = document.createElement('div');
    empty.className = 'filter-empty';
    empty.hidden = true;
    var status = document.createElement('p');
    status.setAttribute('role', 'status');
    status.textContent = 'No encontramos registros con esa búsqueda.';
    var reset = document.createElement('button');
    reset.type = 'button'; reset.className = 'btn btn-sm'; reset.textContent = 'Limpiar búsqueda';
    empty.appendChild(status); empty.appendChild(reset); table.parentElement.appendChild(empty);
    input.addEventListener('input', function () {
      var q = input.value.trim().toLowerCase(), found = 0;
      var rows = $$('tbody tr', table);
      if (!rows.length) rows = $$('[data-filter-row]', table);
      rows.forEach(function (tr) {
        tr.hidden = Boolean(q && tr.textContent.toLowerCase().indexOf(q) === -1);
        if (!tr.hidden) found += 1;
      });
      empty.hidden = !q || found > 0;
    });
    reset.addEventListener('click', function () { input.value = ''; input.dispatchEvent(new Event('input')); input.focus(); });
  });

  // Formsets: agregar y quitar filas.
  $$('[data-formset]').forEach(function (box) {
    var prefix = box.getAttribute('data-formset');
    var total = $('#id_' + prefix + '-TOTAL_FORMS');
    var tpl = $('template', box);
    var body = $('tbody', box);
    var add = $('[data-add-row]', box);
    var max = parseInt(($('#id_' + prefix + '-MAX_NUM_FORMS') || {}).value || '1000', 10);
    function wire(row) {
      $$('.del input[type=checkbox]', row).forEach(function (chk) {
        chk.addEventListener('change', function () { row.classList.toggle('removed', chk.checked); recalc(); });
      });
      $$('input, select', row).forEach(function (el) { el.addEventListener('input', recalc); el.addEventListener('change', recalc); });
      var product = $('select[name$="-product"]', row);
      var desc = $('input[name$="-description"]', row);
      if (product && desc) {
        product.addEventListener('change', function () {
          if (!desc.value && product.selectedIndex > 0) desc.value = product.options[product.selectedIndex].text.split(' · ')[0];
        });
      }
    }
    $$('tbody tr', box).forEach(wire);
    if (add && tpl && total) {
      add.addEventListener('click', function () {
        var n = parseInt(total.value, 10);
        if (n >= max) return;
        var holder = document.createElement('tbody');
        holder.innerHTML = tpl.innerHTML.replace(/__prefix__/g, n);
        var row = holder.firstElementChild;
        body.appendChild(row);
        total.value = n + 1;
        wire(row);
        var first = $('select, input:not([type=hidden])', row);
        if (first) first.focus();
        recalc();
      });
    }
  });

  // Totales de la factura en vivo.
  function recalc() {
    var box = $('[data-invoice-lines]');
    if (!box) return;
    var sub = 0, tax = 0;
    $$('tbody tr', box).forEach(function (row) {
      if (row.classList.contains('removed')) return;
      var q = num(($('input[name$="-quantity"]', row) || {}).value);
      var p = num(($('input[name$="-unit_price"]', row) || {}).value);
      var d = num(($('input[name$="-discount"]', row) || {}).value);
      var r = num(($('select[name$="-tax_rate"]', row) || {}).value);
      var net = Math.round((q * p - d) * 100) / 100;
      var t = Math.round(net * r) / 100;
      sub += net; tax += t;
      var out = $('[data-line-total]', row);
      if (out) out.textContent = q ? cop(net + t) : '—';
    });
    var gd = num(($('#id_global_discount') || {}).value), ret = num(($('#id_retention') || {}).value);
    var set = function (id, v) { var el = $(id); if (el) el.textContent = cop(v); };
    set('#t-subtotal', sub); set('#t-discount', -gd); set('#t-tax', tax); set('#t-retention', -ret); set('#t-total', sub - gd + tax - ret);
    var check = $('[data-payable]');
    if (check) {
      var pdfTotal = $('#id_pdf_total');
      var expected = num(pdfTotal ? pdfTotal.value : check.getAttribute('data-payable'));
      var diff = Math.abs(expected - (sub - gd + tax - ret));
      var mismatch = diff >= (pdfTotal ? 0.005 : 1);
      check.classList.toggle('mismatch', mismatch);
      check.textContent = pdfTotal && !pdfTotal.value ? 'Complete el total a pagar que aparece en el PDF.' : 'Total del documento: ' + cop(expected) + (mismatch ? ' · no coincide con el calculado, revise valores y descuentos' : ' · coincide');
    }
  }
  ['#id_global_discount', '#id_retention', '#id_pdf_total'].forEach(function (id) { var el = $(id); if (el) el.addEventListener('input', recalc); });
  recalc();

  // Vista previa del descuento por pronto pago según la fecha del pago.
  var pay = $('[data-payment]');
  if (pay) {
    var issue = new Date(pay.getAttribute('data-issue') + 'T00:00:00');
    var base = num(pay.getAttribute('data-base')), total = num(pay.getAttribute('data-total'));
    var tiers = JSON.parse(pay.getAttribute('data-tiers') || '[]').map(function (t) { return [parseInt(t[0], 10), num(t[1])]; }).sort(function (a, b) { return a[0] - b[0]; });
    var input = $('#id_paid_on');
    var update = function () {
      if (!input || !input.value) return;
      var days = Math.round((new Date(input.value + 'T00:00:00') - issue) / 86400000);
      var tier = tiers.filter(function (t) { return days <= t[0]; })[0];
      var percent = tier ? tier[1] : 0;
      var discount = Math.round(base * percent) / 100;
      $('#p-days').textContent = days < 0 ? '—' : days + (days === 1 ? ' día' : ' días');
      $('#p-percent').textContent = String(percent).replace('.', ',') + ' %';
      $('#p-discount').textContent = cop(discount);
      $('#p-due').textContent = cop(total - discount);
    };
    input.addEventListener('input', update);
    input.addEventListener('change', update);
    update();
  }

  // Vista previa del descuento de una venta (el servidor recalcula y valida al guardar).
  var discountForm = $('[data-discount-form]');
  if (discountForm) {
    var percentInput = $('#porcentaje', discountForm);
    var updateDiscount = function () {
      var p = percentInput && percentInput.value !== '' ? Math.min(100, Math.max(0, num(percentInput.value))) : null;
      var gross = 0, off = 0;
      $$('tr[data-gross]', discountForm).forEach(function (row) {
        var g = num(row.getAttribute('data-gross')), field = $('input', row);
        var d = p === null ? Math.min(g, Math.max(0, num(field.value))) : Math.round(g * p) / 100;
        field.disabled = p !== null;
        gross += g; off += d;
      });
      var set = function (sel, v) { var el = $(sel, discountForm); if (el) el.textContent = v; };
      set('[data-discount-gross]', cop(gross));
      set('[data-discount-total]', off ? '−' + cop(off) + ' (' + (gross ? (off * 100 / gross).toFixed(1).replace('.', ',') : '0') + ' %)' : '$0');
      set('[data-discount-net]', cop(gross - off));
    };
    $$('input', discountForm).forEach(function (el) { el.addEventListener('input', updateDiscount); });
    updateDiscount();
  }

  // Muestra la causa solo para bajas.
  var kind = $('#id_kind'), reason = $('#id_loss_reason');
  if (kind && reason) {
    var field = reason.closest('.field');
    var toggle = function () { field.hidden = kind.value !== 'writeoff'; };
    kind.addEventListener('change', toggle);
    toggle();
  }
})();
