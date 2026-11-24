// Escáner de recepción: lector de mano (teclado), cámara del celular y envío sin recargar la página.
// Sin este archivo la pantalla sigue funcionando: cada formulario se envía normalmente.
(function () {
  'use strict';
  var me = document.currentScript;
  var ZXING_URL = me ? me.getAttribute('data-zxing') : '';
  var $ = function (sel, root) { return (root || document).querySelector(sel); };
  var $$ = function (sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); };
  var WANTED = ['data_matrix', 'qr_code', 'ean_13', 'ean_8', 'code_128', 'upc_a', 'pdf417'];

  function bars(root) {
    $$('[data-share]', root).forEach(function (el) {
      var v = Math.max(0, Math.min(100, parseInt(el.getAttribute('data-share'), 10) || 0));
      el.style.width = v + '%';
    });
  }

  // Sonido y vibración breves para confirmar cada lectura sin mirar la pantalla.
  var audio = null;
  function signal(ok) {
    try {
      audio = audio || new (window.AudioContext || window.webkitAudioContext)();
      var o = audio.createOscillator(), g = audio.createGain();
      o.frequency.value = ok ? 1046 : 220;
      g.gain.setValueAtTime(0.12, audio.currentTime);
      g.gain.exponentialRampToValueAtTime(0.0001, audio.currentTime + (ok ? 0.12 : 0.35));
      o.connect(g); g.connect(audio.destination); o.start(); o.stop(audio.currentTime + (ok ? 0.12 : 0.35));
    } catch (e) { /* sin audio */ }
    if (navigator.vibrate) navigator.vibrate(ok ? 40 : [80, 60, 80]);
  }

  function feedback(text, level) {
    var box = $('[data-scan-feedback]');
    if (!box) return;
    box.textContent = text;
    box.className = 'scan-feedback is-' + level;
  }

  function focusScan() {
    var prompt = $('[data-prompt] input:not([type=hidden]), [data-prompt] select');
    var target = prompt || $('[data-scan-form] [data-scan-input]');
    if (target) target.focus({ preventScroll: true });
  }

  function apply(data) {
    [['[data-tally]', 'tally'], ['[data-prompt]', 'prompt'], ['[data-progress]', 'progress']].forEach(function (pair) {
      var el = $(pair[0]);
      if (el && typeof data[pair[1]] === 'string') { el.innerHTML = data[pair[1]]; bars(el); }
    });
    var level = data.ok ? (data.warn ? 'warn' : 'ok') : (data.kind === 'unknown' || data.kind === 'needs_details' || data.kind === 'duplicate' || data.kind === 'invoice' ? 'warn' : 'bad');
    feedback(data.message || '', level);
    signal(data.ok);
    focusScan();
  }

  var busy = false;
  function send(form) {
    if (busy) return Promise.resolve();
    busy = true;
    form.classList.add('is-busy');
    return fetch(form.action, { method: 'POST', body: new FormData(form), credentials: 'same-origin', headers: { 'X-Requested-With': 'fetch' } })
      .then(function (r) {
        if (r.status === 403) throw new Error('Su sesión venció o su perfil no tiene permiso. Vuelva a ingresar.');
        if (!r.ok) throw new Error('No se pudo registrar (error ' + r.status + '). Intente de nuevo.');
        return r.json();
      })
      .then(function (data) {
        apply(data);
        var input = $('[data-scan-input]', form);
        if (input) input.value = '';
        var boxes = $('[name=cajas]', form);
        if (boxes && data.ok) boxes.value = '1';
      })
      .catch(function (err) { feedback(err.message, 'bad'); signal(false); })
      .then(function () { busy = false; form.classList.remove('is-busy'); });
  }

  document.addEventListener('submit', function (e) {
    var form = e.target;
    if (!form.matches('[data-fetch]') || !window.fetch) return;
    e.preventDefault();
    send(form);
  });

  // Lector de mano: escribe muy rápido y termina con Enter. El QR de la factura trae varias líneas (varios Enter):
  // se juntan en una sola lectura y se envía cuando el lector deja de escribir.
  $$('[data-scan-input]').forEach(function (input) {
    var timer = null;
    // El temporizador solo existe entre un Enter y el envío. Al dispararse se anula: así, quien teclea el código a mano
    // (con pausas entre teclas) no provoca envíos a medias; nada se envía sin Enter o sin el botón «Agregar».
    function fire() {
      timer = null;
      if (input.value.trim()) input.form.requestSubmit();
    }
    input.addEventListener('keydown', function (e) {
      if (e.key !== 'Enter') return;
      e.preventDefault();
      if (!input.value.trim()) return;
      input.value += ' ';
      clearTimeout(timer);
      timer = setTimeout(fire, 260);
    });
    input.addEventListener('input', function () { if (timer) { clearTimeout(timer); timer = setTimeout(fire, 260); } });
  });

  // Cámara: detector nativo del navegador si lee DataMatrix; si no, ZXing (incluido en la plataforma, sin servicios externos).
  function loadZXing() {
    if (window.ZXing) return Promise.resolve(window.ZXing);
    return new Promise(function (resolve, reject) {
      var s = document.createElement('script');
      s.src = ZXING_URL;
      s.onload = function () { resolve(window.ZXing); };
      s.onerror = function () { reject(new Error('No se pudo cargar el lector de la cámara.')); };
      document.head.appendChild(s);
    });
  }

  function Camera(button) {
    this.button = button;
    this.box = $('[data-camera-box]');
    this.video = $('[data-camera-video]', this.box);
    this.hint = $('[data-camera-hint]', this.box);
    this.input = $(button.getAttribute('data-target'));
    this.continuous = button.hasAttribute('data-continuous');
    this.last = { text: '', at: 0 };
    this.running = false;
  }
  Camera.prototype.found = function (text) {
    var now = Date.now();
    if (!text || busy) return;
    if (text === this.last.text && now - this.last.at < 3000) return;   // la misma caja sigue frente a la cámara
    this.last = { text: text, at: now };
    this.input.value = text;
    var form = this.input.form;
    if (!this.continuous) this.stop();
    if (form.matches('[data-fetch]')) send(form); else form.requestSubmit();
  };
  Camera.prototype.start = function () {
    var self = this;
    this.running = true;
    this.box.hidden = false;
    this.button.classList.add('is-on');
    this.button.lastChild.textContent = 'Apagar la cámara';
    this.hint.textContent = 'Apunte al código';
    var native = 'BarcodeDetector' in window ? window.BarcodeDetector.getSupportedFormats().catch(function () { return []; }) : Promise.resolve([]);
    return native.then(function (formats) {
      var use = WANTED.filter(function (f) { return formats.indexOf(f) !== -1; });
      if (use.indexOf('data_matrix') !== -1) return self.startNative(use);
      return self.startZXing();
    }).catch(function (err) { self.fail(err); });
  };
  Camera.prototype.startNative = function (formats) {
    var self = this;
    var detector = new window.BarcodeDetector({ formats: formats });
    return navigator.mediaDevices.getUserMedia({ video: { facingMode: { ideal: 'environment' } }, audio: false }).then(function (stream) {
      self.stream = stream;
      self.video.srcObject = stream;
      return self.video.play();
    }).then(function () {
      var tick = function () {
        if (!self.running) return;
        detector.detect(self.video).then(function (codes) {
          if (codes.length) self.found(codes[0].rawValue);
        }).catch(function () {}).then(function () { if (self.running) self.timer = setTimeout(tick, 180); });
      };
      tick();
    });
  };
  Camera.prototype.startZXing = function () {
    var self = this;
    return loadZXing().then(function (Z) {
      var hints = new Map();
      hints.set(Z.DecodeHintType.POSSIBLE_FORMATS, [Z.BarcodeFormat.DATA_MATRIX, Z.BarcodeFormat.QR_CODE, Z.BarcodeFormat.EAN_13, Z.BarcodeFormat.EAN_8,
        Z.BarcodeFormat.CODE_128, Z.BarcodeFormat.UPC_A]);
      hints.set(Z.DecodeHintType.TRY_HARDER, true);
      self.reader = new Z.BrowserMultiFormatReader(hints, 250);
      return self.reader.decodeFromConstraints({ video: { facingMode: { ideal: 'environment' } } }, self.video, function (result) {
        if (result && self.running) self.found(result.getText());
      });
    });
  };
  Camera.prototype.stop = function () {
    this.running = false;
    clearTimeout(this.timer);
    if (this.reader) { try { this.reader.reset(); } catch (e) { /* ya detenido */ } this.reader = null; }
    if (this.stream) { this.stream.getTracks().forEach(function (t) { t.stop(); }); this.stream = null; }
    this.video.srcObject = null;
    this.box.hidden = true;
    this.button.classList.remove('is-on');
    this.button.lastChild.textContent = 'Usar la cámara';
  };
  Camera.prototype.fail = function (err) {
    var msg = err && err.name === 'NotAllowedError' ? 'El navegador no tiene permiso para usar la cámara. Actívelo en los ajustes del sitio.'
      : err && err.name === 'NotFoundError' ? 'No se encontró una cámara en este equipo.' : (err && err.message) || 'No se pudo abrir la cámara.';
    this.stop();
    feedback(msg, 'bad');
  };

  $$('[data-camera-toggle]').forEach(function (button) {
    if (!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia) || !window.isSecureContext) return;   // la cámara exige HTTPS
    button.hidden = false;
    var cam = new Camera(button);
    button.addEventListener('click', function () { if (cam.running) cam.stop(); else cam.start(); });
    window.addEventListener('pagehide', function () { if (cam.running) cam.stop(); });
  });

  bars(document);
})();
