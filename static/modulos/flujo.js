// Mapa de flujo: cambia de estación sin recargar. Sin este archivo cada estación es un enlace (?etapa=) y la
// página llega del servidor con su panel abierto. Solo presentación: los conteos y permisos vienen del servidor.
(function () {
  'use strict';
  var map = document.querySelector('[data-flujo]');
  if (!map) return;
  var tabs = Array.prototype.slice.call(map.querySelectorAll('[role="tab"]'));
  if (!tabs.length) return;
  var vertical = window.matchMedia('(max-width: 760px)');

  function panelOf(tab) { return document.getElementById(tab.getAttribute('aria-controls')); }

  function select(tab, opts) {
    opts = opts || {};
    var changed = tab.getAttribute('aria-selected') !== 'true';
    // El panel solo se anima cuando la persona lo pidió con el puntero; con el teclado el cambio es inmediato.
    map.classList.toggle('is-animated', Boolean(opts.animate) && changed);
    tabs.forEach(function (t) {
      var on = t === tab, panel = panelOf(t);
      t.setAttribute('aria-selected', String(on));
      t.tabIndex = on ? 0 : -1;
      if (panel) panel.hidden = !on;
    });
    if (opts.focus) tab.focus();
    if (changed && window.history && window.history.replaceState) {
      var url = new URL(window.location.href);
      url.searchParams.set('etapa', tab.getAttribute('data-etapa'));
      url.hash = '';
      window.history.replaceState(null, '', url);
    }
    // En celular el panel se abre debajo de su estación: se deja a la vista sin perder la estación.
    if (changed && opts.animate && vertical.matches && tab.scrollIntoView) {
      tab.scrollIntoView({ block: 'nearest', behavior: 'auto' });
    }
  }

  tabs.forEach(function (tab, i) {
    tab.addEventListener('click', function (event) {
      if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0) return;
      event.preventDefault();
      // Un clic nacido del teclado (Enter) llega con detail 0.
      select(tab, { animate: event.detail > 0 });
    });
    tab.addEventListener('keydown', function (event) {
      var next = { ArrowRight: i + 1, ArrowDown: i + 1, ArrowLeft: i - 1, ArrowUp: i - 1, Home: 0, End: tabs.length - 1 }[event.key];
      if (event.key === ' ') { event.preventDefault(); select(tab); return; }
      if (next === undefined) return;
      event.preventDefault();
      select(tabs[(next + tabs.length) % tabs.length], { focus: true });
    });
  });
  map.addEventListener('animationend', function (event) {
    if (event.animationName === 'flujo-entra') map.classList.remove('is-animated');
  });
})();
