/* Copiloto: envía la pregunta sin recargar. Sin este archivo los formularios funcionan igual (envío normal).
   La respuesta llega ya dibujada por el servidor; aquí solo se inserta, se muestra la espera y se explican los errores. */
(function () {
  'use strict';
  var chat = document.querySelector('[data-copilot]');
  if (!chat || !window.fetch || !window.FormData) return;
  var log = chat.querySelector('[data-copilot-log]');
  var main = chat.querySelector('[data-copilot-main]');
  var box = main ? main.querySelector('textarea[name="q"]') : null;
  var url = chat.getAttribute('data-url');
  var busy = false;
  var reduced = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text) node.textContent = text;
    return node;
  }

  function bubble(cls, who, lines) {
    var b = el('div', 'bubble ' + cls);
    b.appendChild(el('span', 'bubble-meta', who));
    lines.forEach(function (line) { b.appendChild(el('p', '', line)); });
    return b;
  }

  function show(node) {
    if (node && node.scrollIntoView) node.scrollIntoView({ block: 'nearest', behavior: reduced ? 'auto' : 'smooth' });
  }

  function setBusy(on) {
    busy = on;
    chat.classList.toggle('is-busy', on);
    Array.prototype.forEach.call(document.querySelectorAll('form[data-copilot-form] button[type=submit]'), function (b) {
      if (on) b.setAttribute('aria-busy', 'true'); else b.removeAttribute('aria-busy');
    });
  }

  function grow() {
    if (!box) return;
    box.style.height = 'auto';
    box.style.height = Math.min(box.scrollHeight + 2, 160) + 'px';
  }

  function fail(pending, question, message) {
    if (pending.wait.parentNode) pending.wait.parentNode.removeChild(pending.wait);
    var lines = [message];
    if (box) {
      if (!box.value.trim()) box.value = question;
      grow();
      lines.push('Su pregunta sigue en el cuadro de abajo: puede corregirla o enviarla otra vez.');
    }
    var err = bubble('is-bot is-error', 'Copiloto', lines);
    err.setAttribute('role', 'alert');
    log.appendChild(err);
    show(err);
  }

  function send(data, question) {
    var empty = log.querySelector('[data-copilot-empty]');
    if (empty) empty.parentNode.removeChild(empty);
    var mine = bubble('is-user is-new', 'Usted', [question]);
    var wait = el('div', 'bubble is-bot copilot-pending');
    wait.appendChild(el('span', 'bubble-meta', 'Copiloto · consultando los datos…'));
    wait.appendChild(el('span', 'skeleton'));
    wait.appendChild(el('span', 'skeleton is-short'));
    log.appendChild(mine);
    log.appendChild(wait);
    show(wait);
    var pending = { mine: mine, wait: wait };
    var controller = window.AbortController ? new AbortController() : null;
    var timer = controller ? window.setTimeout(function () { controller.abort(); }, 45000) : null;
    setBusy(true);
    fetch(url, { method: 'POST', body: data, credentials: 'same-origin', signal: controller ? controller.signal : undefined,
                 headers: { 'X-Requested-With': 'fetch', 'Accept': 'application/json' } })
      .then(function (response) {
        var type = response.headers.get('Content-Type') || '';
        if (type.indexOf('application/json') === -1) {
          var why = response.status === 403 ? 'La sesión o el formulario vencieron. Recargue la página e intente de nuevo.'
            : 'El servidor no devolvió una respuesta válida. Recargue la página; si la sesión venció, vuelva a ingresar.';
          throw new Error(why);
        }
        return response.json().then(function (body) {
          if (!body.ok) throw new Error(body.error || 'No se pudo responder la pregunta.');
          return body;
        });
      })
      .then(function (body) {
        var holder = document.createElement('div');
        holder.innerHTML = body.html;              // fragmento renderizado y escapado por el servidor
        log.removeChild(mine);
        log.removeChild(wait);
        var answer = null;
        while (holder.firstChild) {
          var node = holder.firstChild;
          log.appendChild(node);
          if (node.nodeType === 1) {
            node.classList.add('is-new');
            if (node.hasAttribute('data-copilot-answer')) answer = node;
          }
        }
        if (box && box.value.trim() === question) { box.value = ''; grow(); }
        show(answer);
      })
      .catch(function (error) {
        var message = error && error.name === 'AbortError' ? 'La consulta tardó demasiado y se canceló.'
          : (error && error.message && error.name !== 'TypeError' ? error.message : 'No hubo conexión con el servidor.');
        fail(pending, question, message);
      })
      .then(function () {
        if (timer) window.clearTimeout(timer);
        setBusy(false);
      });
  }

  document.addEventListener('submit', function (event) {
    var form = event.target;
    if (!form || !form.matches || !form.matches('form[data-copilot-form]')) return;
    event.preventDefault();
    if (busy) return;
    var data = new FormData(form);
    var submitter = event.submitter;
    if (submitter && submitter.name) data.set(submitter.name, submitter.value);
    var question = String(data.get('q') || '').trim();
    if (!question) { if (box) box.focus(); return; }
    send(data, question);
  });

  if (box) {
    box.addEventListener('input', grow);
    box.addEventListener('keydown', function (event) {
      if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
        event.preventDefault();
        if (main.requestSubmit) main.requestSubmit(); else main.querySelector('button[type=submit]').click();
      }
    });
    grow();
  }
})();
