// Indicador de carga entre páginas y durante envíos.
// Solo presentación. Las reglas, permisos y confirmaciones dependen siempre del servidor.
(function () {
  'use strict';
  // Aparece si la espera supera 120 ms y se retira al volver a la página o tras 8 s
  // (descargas que no reemplazan la página).
  var bar = document.querySelector('.page-progress');
  var showTimer = 0, guardTimer = 0;
  var DOWNLOAD = /\.(csv|xlsx|pdf)$|\/soportes?\//i;
  function stopLoading() {
    window.clearTimeout(showTimer); window.clearTimeout(guardTimer);
    if (bar) bar.classList.remove('is-loading');
  }
  function startLoading() {
    if (!bar) return;
    stopLoading();
    showTimer = window.setTimeout(function () { bar.classList.add('is-loading'); }, 120);
    guardTimer = window.setTimeout(stopLoading, 8000);
  }
  function isNavigation(url) {
    return url.origin === window.location.origin && !DOWNLOAD.test(url.pathname) &&
      !(url.pathname === window.location.pathname && url.search === window.location.search && url.hash);
  }
  document.addEventListener('click', function (event) {
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    var link = event.target.closest ? event.target.closest('a[href]') : null;
    if (!link || link.target || link.hasAttribute('download')) return;
    if (isNavigation(new URL(link.href, window.location.href))) startLoading();
  });
  // Se registra en document para ejecutarse después de las validaciones de cada formulario.
  document.addEventListener('submit', function (event) {
    if (event.defaultPrevented) return;
    var form = event.target;
    if (form.target) return;
    if (isNavigation(new URL(form.getAttribute('action') || window.location.href, window.location.href))) startLoading();
  });
  window.addEventListener('pageshow', stopLoading);

})();
