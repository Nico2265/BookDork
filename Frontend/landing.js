/* landing.js — Lógica de la landing page (módulo ES) */

/* ── Estado de autenticación → actualiza el botón del nav ─────────────── */
/* Dynamic import: si el CDN falla, el resto del módulo sigue funcionando  */
(function () {
  Promise.all([
    import('/static/firebase.js'),
    import('https://www.gstatic.com/firebasejs/10.14.1/firebase-auth.js'),
  ]).then(function (modules) {
    var auth              = modules[0].auth;
    var onAuthStateChanged = modules[1].onAuthStateChanged;
    onAuthStateChanged(auth, function (user) {
      var btn = document.getElementById('nav-auth-btn');
      if (!btn) return;
      if (user) {
        btn.textContent = 'Ir al buscador →';
        btn.href = '/search';
        btn.classList.add('l-nav__auth--logged');
      }
    });
  }).catch(function () { /* Firebase no disponible — no bloquear el resto */ });
})();

/* ── Partículas ────────────────────────────────────────────────────────── */
(function () {
  var canvas = document.getElementById('particles-canvas');
  if (!canvas) return;
  var ctx = canvas.getContext('2d');
  var W, H, particles = [];

  function resize() {
    W = canvas.width  = window.innerWidth;
    H = canvas.height = window.innerHeight;
  }
  resize();
  window.addEventListener('resize', resize, { passive: true });

  function rand(a, b) { return Math.random() * (b - a) + a; }

  function Particle() {
    this.reset = function () {
      this.x     = rand(0, W);
      this.y     = rand(0, H);
      this.size  = rand(0.8, 2.0);
      this.vx    = rand(-0.1, 0.1);
      this.vy    = rand(-0.1, 0.1);
      this.alpha = rand(0.06, 0.28);
      this.gold  = Math.random() > 0.8;
    };
    this.reset();
    this.update = function () {
      this.x += this.vx;
      this.y += this.vy;
      if (this.x < 0 || this.x > W || this.y < 0 || this.y > H) this.reset();
    };
    this.draw = function () {
      ctx.beginPath();
      ctx.arc(this.x, this.y, this.size, 0, Math.PI * 2);
      ctx.fillStyle = this.gold
        ? 'rgba(201,150,90,' + this.alpha + ')'
        : 'rgba(237,234,226,' + (this.alpha * 0.3) + ')';
      ctx.fill();
    };
  }

  for (var i = 0; i < 130; i++) particles.push(new Particle());

  (function loop() {
    ctx.clearRect(0, 0, W, H);
    for (var j = 0; j < particles.length; j++) {
      particles[j].update();
      particles[j].draw();
    }
    requestAnimationFrame(loop);
  })();
})();

/* ── Nav blur al hacer scroll ──────────────────────────────────────────── */
(function () {
  var nav = document.querySelector('.l-nav');
  if (!nav) return;
  window.addEventListener('scroll', function () {
    nav.classList.toggle('l-nav--scrolled', window.scrollY > 50);
  }, { passive: true });
})();

/* ── Scroll reveal ─────────────────────────────────────────────────────── */
(function () {
  var observer = new IntersectionObserver(function (entries) {
    entries.forEach(function (e) {
      if (e.isIntersecting) {
        e.target.classList.add('revealed');
        observer.unobserve(e.target);
      }
    });
  }, { threshold: 0.1 });
  document.querySelectorAll('.reveal').forEach(function (el) {
    observer.observe(el);
  });
})();

/* ── Animación de contadores de tokens ─────────────────────────────────── */
(function () {
  var BEFORE  = 418000;
  var AFTER   = 73000;
  var SAVING  = Math.round((1 - AFTER / BEFORE) * 100);
  var done    = false;

  function fmt(n) { return n.toLocaleString('es-CL'); }

  function animateCount(el, from, to, duration, format) {
    var start = performance.now();
    (function step(now) {
      var t    = Math.min((now - start) / duration, 1);
      var ease = t < 0.5 ? 2 * t * t : -1 + (4 - 2 * t) * t;
      el.textContent = format(Math.round(from + (to - from) * ease));
      if (t < 1) requestAnimationFrame(step);
    })(start);
  }

  var observer = new IntersectionObserver(function (entries) {
    entries.forEach(function (e) {
      if (!e.isIntersecting || done) return;
      done = true;
      observer.unobserve(e.target);

      var elBefore = document.getElementById('count-before');
      var elAfter  = document.getElementById('count-after');
      var elPct    = document.getElementById('saving-pct');
      var elMeter  = document.getElementById('meter-after');

      if (elBefore) animateCount(elBefore, 0, BEFORE, 1600, fmt);
      setTimeout(function () {
        if (elAfter)  animateCount(elAfter, BEFORE, AFTER, 1200, fmt);
        if (elPct)    animateCount(elPct, 0, SAVING, 1200, function (n) { return n + '%'; });
        if (elMeter) {
          elMeter.style.transition = 'width 1.2s cubic-bezier(.4,0,.2,1)';
          elMeter.style.width = (AFTER / BEFORE * 100).toFixed(1) + '%';
        }
      }, 900);
    });
  }, { threshold: 0.25 });

  var section = document.querySelector('.l-tokens');
  if (section) observer.observe(section);
})();
