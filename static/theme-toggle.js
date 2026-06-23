/* =====================================================================
   theme-toggle.js — Switch claro/oscuro con persistencia (localStorage)
   ---------------------------------------------------------------------
   Uso:
   1) En <head>, ANTES del CSS, pega el snippet anti-parpadeo (ver README).
   2) Carga este archivo al final del <body>:
        <script src="{{ url_for('static', filename='theme-toggle.js') }}"></script>
   3) El botón debe tener  data-theme-toggle  y, opcionalmente, hijos con
      data-theme-icon  y  data-theme-label  para actualizar texto/ícono.

        <button class="theme-toggle" data-theme-toggle>
          <span data-theme-icon>☾</span><span data-theme-label>Oscuro</span>
        </button>
   ===================================================================== */
(function () {
  var KEY = 'quiniela:theme';
  var root = document.documentElement;

  function current() {
    return root.getAttribute('data-theme') === 'dark' ? 'dark' : 'light';
  }

  function sync() {
    var dark = current() === 'dark';
    document.querySelectorAll('[data-theme-icon]').forEach(function (el) {
      el.textContent = dark ? '☀' : '☾';
    });
    document.querySelectorAll('[data-theme-label]').forEach(function (el) {
      el.textContent = dark ? 'Claro' : 'Oscuro';
    });
  }

  function apply(theme) {
    root.setAttribute('data-theme', theme);
    try { localStorage.setItem(KEY, theme); } catch (e) {}
    sync();
  }

  // Estado inicial: localStorage > preferencia del sistema > claro
  function init() {
    var saved;
    try { saved = localStorage.getItem(KEY); } catch (e) {}
    if (!saved) {
      saved = (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) ? 'dark' : 'light';
    }
    root.setAttribute('data-theme', saved);
    sync();
  }

  document.addEventListener('click', function (e) {
    var btn = e.target.closest('[data-theme-toggle]');
    if (!btn) return;
    apply(current() === 'dark' ? 'light' : 'dark');
  });

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
