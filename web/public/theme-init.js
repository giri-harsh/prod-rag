// Applies the saved colour theme before first paint. Kept as a file (not inline) because the CSP forbids inline scripts.
(function () {
  try {
    var t = window.localStorage.getItem('rag.theme');
    if (t === 'light' || t === 'dark') document.documentElement.setAttribute('data-theme', t);
  } catch (e) {
    /* storage blocked: follow the system setting */
  }
})();
