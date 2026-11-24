// Facilita el registro existente. La disponibilidad, los permisos y el guardado se validan en Django.
(function () {
  'use strict';

  var flows = {
    new_request: {
      label: 'Solicitud de referencias', confirm: 'Confirmar solicitud',
      effect: 'Al confirmar, se enviará la solicitud a inventario. El equipo revisará la disponibilidad, reservará el lote y confirmará la entrega.',
      steps: [
        { label: 'Referencia', title: '¿Qué referencia necesita?', hint: 'Seleccione la referencia que necesita para el área, el pedido o la orden de trabajo.', fields: ['product'] },
        { label: 'Cantidad y fecha', title: '¿Cuántas unidades y para cuándo?', hint: 'Indique la cantidad solicitada. Puede dejar la fecha vacía si aún no está definida.', fields: ['quantity', 'needed_by'] },
        { label: 'Destino', title: '¿A qué destino van las unidades?', hint: 'Escriba el área, el pedido o la orden de trabajo. No incluya datos personales.', fields: ['purpose'] }
      ]
    },
    exit_stock: {
      label: 'Salida de inventario', confirm: 'Confirmar salida',
      effect: 'Al confirmar, se descontarán las unidades disponibles del lote y quedará un movimiento en su trazabilidad. El servidor comprobará la disponibilidad actual.',
      steps: [
        { label: 'Lote', title: '¿De qué lote saldrán las unidades?', hint: 'Revise la referencia, la ubicación y el vencimiento que aparecen en cada opción.', fields: ['batch'] },
        { label: 'Cantidad', title: '¿Cuántas unidades va a retirar?', hint: 'Indique únicamente las unidades de esta salida. Las unidades reservadas se entregan desde Reservas.', fields: ['quantity'] },
        { label: 'Soporte y destino', title: 'Deje la salida identificada', hint: 'Puede añadir el número del documento soporte y el destino u observación. No incluya datos personales.', fields: ['reference', 'note'] }
      ]
    },
    reserve: {
      label: 'Reserva de unidades', confirm: 'Confirmar reserva',
      effect: 'Al confirmar, se apartarán las unidades disponibles. El inventario físico se descontará cuando se confirme la entrega desde Reservas.',
      steps: [
        { label: 'Lote', title: '¿Qué lote quiere reservar?', hint: 'Seleccione la referencia y revise el vencimiento y las unidades disponibles.', fields: ['batch'] },
        { label: 'Cantidad', title: '¿Cuántas unidades va a apartar?', hint: 'La reserva reduce las unidades disponibles para otras operaciones.', fields: ['quantity'] },
        { label: 'Destino', title: '¿Para qué destino es la reserva?', hint: 'Indique el área, el pedido o la orden de trabajo. No incluya datos personales.', fields: ['note'] }
      ]
    },
    adjust_in: {
      label: 'Saldo inicial o ajuste de entrada', confirm: 'Confirmar entrada',
      effect: 'Al confirmar, se sumarán las unidades al inventario físico y se registrará el movimiento por lote. Las recepciones de compras se registran desde la factura del proveedor.',
      steps: [
        { label: 'Tipo', title: '¿Qué entrada va a registrar?', hint: 'El saldo inicial corresponde al conteo de apertura. Un ajuste positivo documenta una diferencia encontrada en un conteo posterior.', fields: ['kind'] },
        { label: 'Referencia y lote', title: 'Identifique la referencia y el lote', hint: 'Copie el lote y la fecha de vencimiento del empaque o soporte del conteo.', fields: ['product', 'lot', 'expiry'] },
        { label: 'Existencias', title: '¿Dónde están y cuántas unidades son?', hint: 'Indique la ubicación física y las unidades que entrarán en este movimiento.', fields: ['location', 'quantity'] },
        { label: 'Observación', title: 'Deje el motivo del registro', hint: 'Para un ajuste positivo, explique la diferencia del conteo. En un saldo inicial la observación es opcional.', fields: ['note'] }
      ]
    },
    adjust_out: {
      label: 'Baja o ajuste de salida', confirm: 'Confirmar baja o ajuste',
      effect: 'Al confirmar, se descontarán las unidades libres del lote y quedará registrada la justificación. El servidor comprobará el saldo y las reservas activas.',
      steps: [
        { label: 'Tipo y lote', title: '¿Qué movimiento necesita registrar?', hint: 'Seleccione el tipo de salida y el lote al que corresponde.', fields: ['kind', 'batch'] },
        { label: 'Cantidad y causa', title: 'Precise las unidades del movimiento', hint: 'Para una baja, indique también la causa. La cantidad debe corresponder a las unidades que se retirarán del inventario.', fields: ['quantity', 'loss_reason'] },
        { label: 'Justificación', title: '¿Por qué se realiza este movimiento?', hint: 'Deje una explicación que permita verificar el registro en la trazabilidad del lote.', fields: ['note'] }
      ]
    }
  };

  Array.prototype.forEach.call(document.querySelectorAll('form[data-operation-guide]'), function (form) {
    var flow = flows[form.getAttribute('data-operation-guide')];
    if (!flow) return;
    var find = function (selector) { return form.querySelector(selector); };
    var wrappers = {};
    Array.prototype.forEach.call(form.querySelectorAll('[data-field-name]'), function (field) {
      wrappers[field.getAttribute('data-field-name')] = field;
    });
    var names = [];
    flow.steps.forEach(function (step) { names = names.concat(step.fields); });
    // Si el formulario cambia, conserva el formulario completo y su envío normal.
    if (names.some(function (name) { return !wrappers[name] || !form.elements.namedItem(name); }) ||
        Object.keys(wrappers).some(function (name) { return names.indexOf(name) === -1; })) return;
    var header = find('[data-guide-header]'), fields = find('[data-guide-fields]');
    var controls = find('[data-guide-controls]'), submit = find('[data-guide-submit]');
    var question = find('[data-guide-question]'), hint = find('[data-guide-hint]');
    var previous = find('[data-guide-previous]'), next = find('[data-guide-next]');
    var review = find('[data-guide-review]'), summary = find('[data-guide-summary]');
    var progress = find('[data-guide-progress]'), count = find('[data-guide-count]');
    var stepsList = find('[data-guide-steps]'), live = find('[data-guide-live]');
    var error = find('[data-guide-error]');
    if (!header || !fields || !controls || !submit || !question || !review || !summary) return;
    var mode = 'guided', position = 0, returnMode = 'guided', furthest = 0;
    var total = flow.steps.length + 1;
    var stepButtons = [];
    var modeButtons = form.querySelectorAll('[data-guide-mode-button]');
    var input = function (name) { return form.elements.namedItem(name); };
    var active = function (name) { return name !== 'loss_reason' || input('kind').value === 'writeoff'; };
    var stepOf = function (name) { return flow.steps.findIndex(function (step) { return step.fields.indexOf(name) !== -1; }); };
    var clearError = function () { error.textContent = ''; error.hidden = true; };

    function updateRequirements() {
      if (wrappers.loss_reason) input('loss_reason').required = input('kind').value === 'writeoff';
      if (wrappers.note && form.getAttribute('data-operation-guide') === 'adjust_in') {
        input('note').required = input('kind').value === 'adjust_in';
        var optional = wrappers.note.querySelector('.optional');
        if (optional) optional.hidden = input('note').required;
      }
      if (wrappers.loss_reason) {
        var reasonOptional = wrappers.loss_reason.querySelector('.optional');
        if (reasonOptional) reasonOptional.hidden = input('loss_reason').required;
      }
    }

    function invalidField(checkNames) {
      updateRequirements();
      for (var i = 0; i < checkNames.length; i++) {
        var name = checkNames[i], control = input(name);
        control.setCustomValidity('');
        if (!active(name) || control.disabled) continue;
        if (control.required && typeof control.value === 'string' && !control.value.trim()) {
          control.setCustomValidity('Complete este campo para continuar.');
        }
        if (!control.checkValidity()) return control;
      }
      return null;
    }

    function validate(checkNames) {
      var invalid = invalidField(checkNames);
      if (!invalid) { clearError(); return true; }
      if (mode !== 'full') { mode = 'guided'; position = stepOf(invalid.name); }
      render(false);
      error.textContent = invalid.validationMessage;
      error.hidden = false;
      invalid.setAttribute('aria-invalid', 'true');
      invalid.focus();
      invalid.reportValidity();
      return false;
    }

    function valueText(control) {
      if (control.tagName === 'SELECT') {
        return control.value ? control.options[control.selectedIndex].textContent.trim() : 'No indicado';
      }
      if (control.type === 'date' && /^\d{4}-\d{2}-\d{2}$/.test(control.value)) {
        return control.value.split('-').reverse().join('/');
      }
      return control.value.trim() || 'No indicado';
    }

    function buildSummary() {
      summary.replaceChildren();
      names.filter(active).forEach(function (name) {
        var row = document.createElement('div'), term = document.createElement('dt'), value = document.createElement('dd');
        var label = wrappers[name].querySelector('label').cloneNode(true);
        var optional = label.querySelector('.optional');
        if (optional) optional.remove();
        term.textContent = label.textContent.trim();
        // Las respuestas siempre se insertan como texto, nunca como HTML.
        value.textContent = valueText(input(name));
        row.appendChild(term); row.appendChild(value); summary.appendChild(row);
      });
    }

    function render(focus) {
      updateRequirements();
      var isReview = mode === 'review', isFull = mode === 'full';
      form.setAttribute('data-guide-mode', mode);
      fields.hidden = isReview;
      names.forEach(function (name) {
        wrappers[name].hidden = !active(name) || (!isFull && (isReview || flow.steps[position].fields.indexOf(name) === -1));
      });
      review.hidden = !isReview;
      submit.hidden = !isReview;
      next.hidden = isReview;
      previous.hidden = isFull || (!isReview && position === 0);
      stepsList.hidden = isFull;
      progress.hidden = isFull;
      count.hidden = isFull;
      var current = isReview ? total : position + 1;
      progress.value = current;
      count.textContent = 'Paso ' + current + ' de ' + total;
      question.textContent = isReview ? 'Revise y confirme el registro' : isFull ? 'Complete la información de la operación' : flow.steps[position].title;
      hint.textContent = isReview ? 'Compruebe las respuestas. El registro se enviará únicamente al pulsar el botón de confirmación.' : isFull ? 'Puede editar todos los campos juntos y revisar el resumen antes de confirmar.' : flow.steps[position].hint;
      next.textContent = isFull || position === flow.steps.length - 1 ? 'Revisar registro' : 'Continuar';
      Array.prototype.forEach.call(modeButtons, function (button) {
        button.setAttribute('aria-pressed', String(button.getAttribute('data-guide-mode-button') === (isReview ? returnMode : mode)));
      });
      stepButtons.forEach(function (button, index) {
        button.disabled = index > furthest;
        button.parentElement.classList.toggle('is-current', index === current - 1);
        button.parentElement.classList.toggle('is-complete', index < current - 1);
        if (index === current - 1) button.setAttribute('aria-current', 'step'); else button.removeAttribute('aria-current');
      });
      if (isReview) buildSummary();
      if (focus) {
        question.focus();
        live.textContent = isFull ? 'Formulario completo.' : 'Paso ' + current + ' de ' + total + '. ' + question.textContent;
      }
    }

    function showReview() {
      if (!validate(names)) return;
      returnMode = mode === 'full' ? 'full' : 'guided';
      mode = 'review'; furthest = flow.steps.length;
      clearError(); render(true);
    }

    function advance() {
      if (mode === 'full') { showReview(); return; }
      if (mode === 'review') return;
      if (!validate(flow.steps[position].fields)) return;
      if (position === flow.steps.length - 1) { showReview(); return; }
      position += 1; furthest = Math.max(furthest, position);
      clearError(); render(true);
    }

    flow.steps.concat([{ label: 'Confirmación' }]).forEach(function (step, index) {
      var item = document.createElement('li'), button = document.createElement('button');
      button.type = 'button';
      button.textContent = (index + 1) + '. ' + step.label;
      button.addEventListener('click', function () {
        if (index > furthest) return;
        if (index === flow.steps.length) { showReview(); return; }
        mode = 'guided'; position = index;
        clearError(); render(true);
      });
      item.appendChild(button); stepsList.appendChild(item); stepButtons.push(button);
    });
    Array.prototype.forEach.call(modeButtons, function (button) {
      button.addEventListener('click', function () {
        mode = button.getAttribute('data-guide-mode-button');
        clearError(); render(true);
      });
    });
    next.addEventListener('click', advance);
    previous.addEventListener('click', function () {
      if (mode === 'review') { mode = returnMode; position = flow.steps.length - 1; }
      else position = Math.max(0, position - 1);
      clearError(); render(true);
    });
    find('[data-guide-edit]').addEventListener('click', function () { mode = 'full'; clearError(); render(true); });
    form.addEventListener('input', function (event) {
      if (!event.target.matches('input, select, textarea')) return;
      event.target.setCustomValidity('');
      event.target.removeAttribute('aria-invalid');
      clearError();
    });
    form.addEventListener('change', function (event) {
      if (event.target.name === 'kind') render(false);
    });
    // Captura antes del bloqueo de doble envío de app.js: avanzar no envía el formulario.
    form.addEventListener('submit', function (event) {
      var wasReview = mode === 'review';
      if (wasReview && event.submitter === submit && validate(names)) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      if (!wasReview) advance();
    }, true);

    find('[data-guide-kicker]').textContent = flow.label;
    find('[data-guide-effect]').textContent = flow.effect;
    submit.textContent = flow.confirm;
    progress.max = total;
    header.hidden = false; controls.hidden = false;
    form.classList.add('guide-enhanced');
    var serverErrors = form.parentElement.querySelector('[data-guide-server-errors]');
    var firstError = names.find(function (name) { return wrappers[name].hasAttribute('data-field-errors'); });
    if (firstError) position = stepOf(firstError);
    if (serverErrors && !firstError) mode = 'full';
    furthest = position;
    render(false);
    if (serverErrors) {
      serverErrors.addEventListener('click', function (event) {
        var anchor = event.target.closest('a[href^="#"]');
        if (!anchor) return;
        var control = document.getElementById(anchor.getAttribute('href').slice(1));
        if (!control || names.indexOf(control.name) === -1) return;
        event.preventDefault();
        mode = 'guided'; position = stepOf(control.name); furthest = Math.max(furthest, position);
        render(false); control.focus();
      });
      // El resumen de errores incluye también errores globales y de tokens ocultos.
      serverErrors.focus();
    }
  });
})();
