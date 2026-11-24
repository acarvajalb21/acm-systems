// Recorrido guiado: tiempo transcurrido en la barra y paso en curso a la vista en el guion.
// Sin este archivo la barra funciona igual: «Anterior», «Siguiente» y «Terminar» son formularios.
(function () {
  'use strict';
  var bar = document.querySelector('[data-recorrido]');
  var clock = bar && bar.querySelector('[data-recorrido-reloj]');
  var started = bar ? parseInt(bar.getAttribute('data-inicio'), 10) : 0;
  if (clock && started > 0) {
    var two = function (n) { return (n < 10 ? '0' : '') + n; };
    var paint = function () {
      var seconds = Math.max(0, Math.floor(Date.now() / 1000) - started);
      if (seconds > 4 * 60 * 60) { clock.hidden = true; return; }
      clock.textContent = two(Math.floor(seconds / 60)) + ':' + two(seconds % 60);
      clock.setAttribute('aria-label', 'Tiempo del recorrido: ' + Math.floor(seconds / 60) + ' minutos');
      clock.hidden = false;
    };
    paint();
    // Cada 5 segundos basta para llevar el ritmo de una presentación de 5 a 7 minutos.
    window.setInterval(function () { if (!document.hidden) paint(); }, 5000);
  }
  // En la página del guion, el paso en curso queda a la vista si la dirección no pide otro.
  var current = document.querySelector('.recorrido-paso.is-current');
  if (current && !window.location.hash && current.scrollIntoView) {
    current.scrollIntoView({ block: 'center', behavior: 'auto' });
  }
})();
