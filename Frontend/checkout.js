/* ============================================================
   checkout.js — Flujo de pago simulado BookDork
   ------------------------------------------------------------
   SIMULACIÓN: ningún dato de tarjeta sale del navegador ni se
   envía a ningún servidor. El "pago" se resuelve en el cliente.

   Estándares QA aplicados:
     · Validación Luhn del número de tarjeta (ISO/IEC 7812)
     · Detección de marca y reglas por marca (longitud, CVV)
     · Validación de vencimiento (mes 1-12, no expirada)
     · Formateo en vivo y restricción a dígitos
     · Estados accesibles: aria-invalid, role=status, aria-live
     · Submit deshabilitado hasta que el formulario sea válido
     · Estado de carga, manejo de error y recibo de éxito
   ============================================================ */
(function () {
  'use strict';

  /* ── Catálogo de planes ─────────────────────────────────────── */
  var PLANS = {
    basic: {
      name: 'Basic',
      price: 4990,
      desc: 'Para lectores habituales y estudiantes.',
      feats: [
        '50 conversiones a Markdown por mes',
        'Archivos de hasta 50 MB',
        'Conversión en paralelo (hasta 5 documentos)',
        'Soporte por correo (48 h)',
        'Acceso a The Info Vault',
      ],
    },
    pro: {
      name: 'Pro',
      price: 9990,
      desc: 'Para investigadores y profesionales.',
      feats: [
        'Conversiones ilimitadas a Markdown',
        'Procesamiento prioritario',
        'Archivos de hasta 200 MB',
        'Soporte por correo (24 h)',
        'Acceso anticipado a nuevas funciones',
      ],
    },
  };

  var TAX_RATE = 0.19; // IVA Chile

  /* ── Utilidades de formato ──────────────────────────────────── */
  function clp(n) {
    return '$' + Math.round(n).toLocaleString('es-CL');
  }
  function onlyDigits(s) {
    return (s || '').replace(/\D/g, '');
  }
  function $(id) { return document.getElementById(id); }

  /* ── Selección de plan desde la URL ─────────────────────────── */
  function getSelectedPlan() {
    var params = new URLSearchParams(window.location.search);
    var key = (params.get('plan') || 'basic').toLowerCase();
    if (!PLANS[key]) key = 'basic';
    return { key: key, plan: PLANS[key] };
  }

  /* ── Render del resumen de pedido ───────────────────────────── */
  function renderSummary(plan) {
    var subtotal = plan.price;
    var tax = subtotal * TAX_RATE;
    var total = subtotal + tax;

    $('sum-plan-name').textContent = plan.name;
    $('sum-plan-desc').textContent = plan.desc;

    var ul = $('sum-feats');
    ul.innerHTML = '';
    plan.feats.forEach(function (f) {
      var li = document.createElement('li');
      li.textContent = f;
      ul.appendChild(li);
    });

    $('sum-subtotal').textContent = clp(subtotal);
    $('sum-tax').textContent = clp(tax);
    $('sum-total').textContent = clp(total) + ' CLP';

    var d = new Date();
    d.setMonth(d.getMonth() + 1);
    $('sum-renew').textContent =
      'Se renueva automáticamente el ' +
      d.toLocaleDateString('es-CL', { day: 'numeric', month: 'long', year: 'numeric' }) +
      ' por ' + clp(total) + ' CLP. Puedes cancelar antes sin costo.';

    $('submit-label').textContent = 'Pagar ' + clp(total) + ' CLP';
    return { subtotal: subtotal, tax: tax, total: total };
  }

  /* ── Detección de marca de tarjeta ──────────────────────────── */
  function detectBrand(num) {
    if (/^4/.test(num)) return 'visa';
    if (/^(5[1-5]|2[2-7])/.test(num)) return 'mastercard';
    if (/^3[47]/.test(num)) return 'amex';
    if (/^(6011|65|64[4-9])/.test(num)) return 'discover';
    if (/^3(0[0-5]|[68])/.test(num)) return 'diners';
    if (/^35/.test(num)) return 'jcb';
    return '';
  }
  var BRAND_LABELS = {
    visa: 'VISA', mastercard: 'Mastercard', amex: 'AMEX',
    discover: 'Discover', diners: 'Diners', jcb: 'JCB',
  };
  // Longitudes válidas y largo de CVV por marca.
  var BRAND_RULES = {
    visa:       { lengths: [16],         cvv: 3 },
    mastercard: { lengths: [16],         cvv: 3 },
    amex:       { lengths: [15],         cvv: 4 },
    discover:   { lengths: [16],         cvv: 3 },
    diners:     { lengths: [14],         cvv: 3 },
    jcb:        { lengths: [16],         cvv: 3 },
    '':         { lengths: [13,14,15,16,19], cvv: 3 },
  };

  /* ── Algoritmo de Luhn ──────────────────────────────────────── */
  function luhnValid(num) {
    if (!/^\d+$/.test(num)) return false;
    var sum = 0, alt = false;
    for (var i = num.length - 1; i >= 0; i--) {
      var d = parseInt(num.charAt(i), 10);
      if (alt) { d *= 2; if (d > 9) d -= 9; }
      sum += d;
      alt = !alt;
    }
    return sum % 10 === 0;
  }

  /* ── Formateadores en vivo ──────────────────────────────────── */
  function groupCardNumber(digits, brand) {
    if (brand === 'amex') {
      // 4-6-5
      return digits.replace(/^(\d{0,4})(\d{0,6})(\d{0,5}).*/, function (_, a, b, c) {
        return [a, b, c].filter(Boolean).join(' ');
      });
    }
    return digits.replace(/(\d{4})(?=\d)/g, '$1 ').trim();
  }
  function formatExpiry(digits) {
    if (digits.length === 0) return '';
    if (digits.length <= 2) return digits;
    return digits.slice(0, 2) + '/' + digits.slice(2, 4);
  }

  /* ── Validadores por campo (retornan '' si OK, o mensaje) ───── */
  var V = {
    email: function (v) {
      v = v.trim();
      if (!v) return 'Ingresa tu correo.';
      // RFC-pragmático: algo@algo.dominio
      if (!/^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/.test(v)) return 'Correo no válido.';
      return '';
    },
    cardNumber: function (v) {
      var d = onlyDigits(v);
      if (!d) return 'Ingresa el número de tarjeta.';
      var brand = detectBrand(d);
      var rules = BRAND_RULES[brand];
      if (d.length < Math.min.apply(null, rules.lengths)) return 'Número incompleto.';
      if (rules.lengths.indexOf(d.length) === -1) return 'Longitud de tarjeta no válida.';
      if (!luhnValid(d)) return 'El número de tarjeta no es válido.';
      return '';
    },
    cardName: function (v) {
      v = v.trim();
      if (!v) return 'Ingresa el nombre del titular.';
      if (v.length < 3) return 'Nombre demasiado corto.';
      if (!/^[\p{L}\s.''-]+$/u.test(v)) return 'Usa solo letras.';
      if (!/\s/.test(v)) return 'Ingresa nombre y apellido.';
      return '';
    },
    expiry: function (v) {
      var d = onlyDigits(v);
      if (d.length < 4) return 'Vencimiento incompleto.';
      var mm = parseInt(d.slice(0, 2), 10);
      var yy = parseInt(d.slice(2, 4), 10);
      if (mm < 1 || mm > 12) return 'Mes inválido (01-12).';
      var now = new Date();
      var curY = now.getFullYear() % 100;
      var curM = now.getMonth() + 1;
      if (yy < curY || (yy === curY && mm < curM)) return 'La tarjeta está vencida.';
      if (yy > curY + 20) return 'Año fuera de rango.';
      return '';
    },
    cvc: function (v, brand) {
      var d = onlyDigits(v);
      var need = BRAND_RULES[brand || ''].cvv;
      if (!d) return 'Ingresa el CVV.';
      if (d.length !== need) return 'El CVV debe tener ' + need + ' dígitos.';
      return '';
    },
    country: function (v) {
      if (!v) return 'Selecciona el país.';
      return '';
    },
    postal: function (v) {
      v = v.trim();
      if (!v) return 'Ingresa el código postal.';
      if (!/^[A-Za-z0-9\s-]{3,12}$/.test(v)) return 'Código postal no válido.';
      return '';
    },
  };

  /* ── Estado de validez por campo ────────────────────────────── */
  var validity = {
    email: false, cardNumber: false, cardName: false,
    expiry: false, cvc: false, country: false, postal: false,
  };

  function setFieldState(input, errorEl, message) {
    var ok = message === '';
    input.classList.toggle('co-input--invalid', !ok);
    input.classList.toggle('co-input--valid', ok && input.value.trim() !== '');
    input.setAttribute('aria-invalid', ok ? 'false' : 'true');
    errorEl.textContent = message;
    return ok;
  }

  function refreshSubmit() {
    var allValid = Object.keys(validity).every(function (k) { return validity[k]; });
    $('submit-btn').disabled = !allValid;
  }

  /* ── Inicialización ─────────────────────────────────────────── */
  document.addEventListener('DOMContentLoaded', function () {
    var sel = getSelectedPlan();
    var amounts = renderSummary(sel.plan);

    var els = {
      email:   $('email'),
      card:    $('card-number'),
      name:    $('card-name'),
      expiry:  $('card-expiry'),
      cvc:     $('card-cvc'),
      country: $('country'),
      postal:  $('postal'),
    };
    var errs = {
      email:   $('email-error'),
      card:    $('card-number-error'),
      name:    $('card-name-error'),
      expiry:  $('card-expiry-error'),
      cvc:     $('card-cvc-error'),
      country: $('country-error'),
      postal:  $('postal-error'),
    };
    var brandEl = $('card-brand');
    var cvcHint = $('cvc-hint');
    var currentBrand = '';

    /* — Email — */
    function valEmail(show) {
      var msg = V.email(els.email.value);
      validity.email = (msg === '');
      if (show || els.email.value) setFieldState(els.email, errs.email, els.email.value ? msg : '');
      refreshSubmit();
    }
    els.email.addEventListener('input', function () { valEmail(false); });
    els.email.addEventListener('blur', function () { valEmail(true); });

    /* — Número de tarjeta (formato + marca en vivo) — */
    els.card.addEventListener('input', function () {
      var digits = onlyDigits(els.card.value);
      currentBrand = detectBrand(digits);
      // Limita longitud al máximo válido de la marca.
      var maxLen = Math.max.apply(null, BRAND_RULES[currentBrand].lengths);
      if (digits.length > maxLen) digits = digits.slice(0, maxLen);

      els.card.value = groupCardNumber(digits, currentBrand);
      brandEl.textContent = BRAND_LABELS[currentBrand] || '';
      brandEl.setAttribute('data-brand', currentBrand);

      // Actualiza pista y maxlength del CVV según la marca.
      var need = BRAND_RULES[currentBrand].cvv;
      cvcHint.textContent = need + ' dígitos';
      els.cvc.setAttribute('maxlength', String(need));
      if (els.cvc.value) valCvc(false);

      var msg = V.cardNumber(els.card.value);
      validity.cardNumber = (msg === '');
      // Sólo mostramos error fuerte cuando alcanza longitud válida; si no, limpiamos.
      setFieldState(els.card, errs.card, validity.cardNumber || digits.length >= 13 ? msg : '');
      refreshSubmit();
    });
    els.card.addEventListener('blur', function () {
      var msg = V.cardNumber(els.card.value);
      validity.cardNumber = (msg === '');
      setFieldState(els.card, errs.card, els.card.value ? msg : '');
      refreshSubmit();
    });

    /* — Nombre del titular — */
    els.name.addEventListener('input', function () {
      var msg = V.cardName(els.name.value);
      validity.cardName = (msg === '');
      refreshSubmit();
    });
    els.name.addEventListener('blur', function () {
      var msg = V.cardName(els.name.value);
      validity.cardName = (msg === '');
      setFieldState(els.name, errs.name, els.name.value ? msg : '');
      refreshSubmit();
    });

    /* — Vencimiento — */
    els.expiry.addEventListener('input', function () {
      els.expiry.value = formatExpiry(onlyDigits(els.expiry.value));
      var msg = V.expiry(els.expiry.value);
      validity.expiry = (msg === '');
      setFieldState(els.expiry, errs.expiry, validity.expiry || onlyDigits(els.expiry.value).length >= 4 ? msg : '');
      refreshSubmit();
    });
    els.expiry.addEventListener('blur', function () {
      var msg = V.expiry(els.expiry.value);
      validity.expiry = (msg === '');
      setFieldState(els.expiry, errs.expiry, els.expiry.value ? msg : '');
      refreshSubmit();
    });

    /* — CVV — */
    function valCvc(show) {
      els.cvc.value = onlyDigits(els.cvc.value);
      var msg = V.cvc(els.cvc.value, currentBrand);
      validity.cvc = (msg === '');
      if (show) setFieldState(els.cvc, errs.cvc, els.cvc.value ? msg : '');
      refreshSubmit();
    }
    els.cvc.addEventListener('input', function () { valCvc(false); });
    els.cvc.addEventListener('blur', function () { valCvc(true); });

    /* — País — */
    els.country.addEventListener('change', function () {
      var msg = V.country(els.country.value);
      validity.country = (msg === '');
      setFieldState(els.country, errs.country, msg);
      refreshSubmit();
    });

    /* — Código postal — */
    els.postal.addEventListener('input', function () {
      var msg = V.postal(els.postal.value);
      validity.postal = (msg === '');
      refreshSubmit();
    });
    els.postal.addEventListener('blur', function () {
      var msg = V.postal(els.postal.value);
      validity.postal = (msg === '');
      setFieldState(els.postal, errs.postal, els.postal.value ? msg : '');
      refreshSubmit();
    });

    /* — Botón de tarjeta de prueba — */
    $('fill-test-card').addEventListener('click', function () {
      els.email.value = els.email.value || 'demo@bookdork.app';
      els.card.value = '4242 4242 4242 4242';
      els.card.dispatchEvent(new Event('input'));
      els.name.value = 'JOHN DOE';
      var d = new Date();
      var mm = ('0' + (d.getMonth() + 1)).slice(-2);
      var yy = ('0' + ((d.getFullYear() + 3) % 100)).slice(-2);
      els.expiry.value = mm + '/' + yy;
      els.cvc.value = '123';
      els.country.value = 'US';
      els.postal.value = '90210';
      // Dispara validación de todos los campos.
      [els.email, els.expiry, els.cvc, els.postal].forEach(function (e) {
        e.dispatchEvent(new Event('input'));
      });
      els.name.dispatchEvent(new Event('input'));
      els.country.dispatchEvent(new Event('change'));
      els.email.dispatchEvent(new Event('blur'));
      els.name.dispatchEvent(new Event('blur'));
      els.cvc.dispatchEvent(new Event('blur'));
      els.postal.dispatchEvent(new Event('blur'));
      els.card.focus();
    });

    /* ── Envío del formulario ─────────────────────────────────── */
    var form = $('payment-form');
    var statusEl = $('form-status');
    var submitBtn = $('submit-btn');
    var submitLabel = $('submit-label');

    form.addEventListener('submit', function (e) {
      e.preventDefault();

      // Re-valida todo y muestra todos los errores (defensa en profundidad).
      valEmail(true);
      els.card.dispatchEvent(new Event('blur'));
      els.name.dispatchEvent(new Event('blur'));
      els.expiry.dispatchEvent(new Event('blur'));
      valCvc(true);
      els.country.dispatchEvent(new Event('change'));
      els.postal.dispatchEvent(new Event('blur'));

      var allValid = Object.keys(validity).every(function (k) { return validity[k]; });
      if (!allValid) {
        statusEl.className = 'co-form-status co-form-status--error';
        statusEl.textContent = 'Revisa los campos marcados antes de continuar.';
        var firstInvalid = form.querySelector('.co-input--invalid');
        if (firstInvalid) firstInvalid.focus();
        return;
      }

      statusEl.className = 'co-form-status';
      statusEl.textContent = '';

      // Estado de carga (simula la pasarela).
      submitBtn.disabled = true;
      submitBtn.classList.add('co-submit--loading');
      submitLabel.innerHTML = '<span class="co-spinner" aria-hidden="true"></span> Procesando pago…';

      setTimeout(function () {
        showSuccess(sel.plan, amounts, onlyDigits(els.card.value), els.email.value.trim());
      }, 1600);
    });

    /* ── Pantalla de éxito ────────────────────────────────────── */
    function showSuccess(plan, amounts, cardDigits, email) {
      var last4 = cardDigits.slice(-4);
      var orderId = 'BD-' + Date.now().toString(36).toUpperCase().slice(-6) +
                    '-' + Math.floor(Math.random() * 9000 + 1000);
      var now = new Date();

      $('success-msg').textContent =
        'Activamos tu plan ' + plan.name + '. Enviamos el comprobante a ' + email + '.';
      $('rcpt-plan').textContent = plan.name + ' (mensual)';
      $('rcpt-amount').textContent = clp(amounts.total) + ' CLP';
      $('rcpt-card').textContent =
        (BRAND_LABELS[currentBrand] || 'Tarjeta') + ' •••• ' + last4;
      $('rcpt-order').textContent = orderId;
      $('rcpt-date').textContent = now.toLocaleString('es-CL', {
        day: '2-digit', month: '2-digit', year: 'numeric',
        hour: '2-digit', minute: '2-digit',
      });

      var overlay = $('success-overlay');
      overlay.hidden = false;
      // Mueve el foco al diálogo para accesibilidad.
      overlay.querySelector('.co-success-cta').focus();
      document.body.style.overflow = 'hidden';
    }
  });
})();
