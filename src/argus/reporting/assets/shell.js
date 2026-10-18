/* Shared ARGUS navigation and appearance controls. */
(() => {
  function init() {
    const layout = document.getElementById('application-layout');
    const toggle = document.getElementById('cs-nav-toggle');
    const collapse = document.getElementById('nav-collapse');
    const narrow = matchMedia('(max-width:1250px)');
    function setNav(open) {
      if (!layout) return;
      layout.classList.toggle('nav-collapsed', !open);
      layout.classList.toggle('nav-open', open && narrow.matches);
      if (toggle) toggle.setAttribute('aria-expanded', String(open));
    }
    if (collapse) collapse.addEventListener('click', () => setNav(false));
    if (toggle) toggle.addEventListener('click', () => setNav(layout.classList.contains('nav-collapsed')));
    setNav(!narrow.matches);
    narrow.addEventListener('change', () => setNav(!narrow.matches));
    document.addEventListener('keydown', event => {
      if (event.key === 'Escape' && layout && layout.classList.contains('nav-open')) {
        setNav(false);
        if (toggle) toggle.focus();
      }
    });
    if (layout) layout.addEventListener('click', event => {
      if (event.target === layout && layout.classList.contains('nav-open')) setNav(false);
    });
    const theme = document.getElementById('cs-theme-toggle');
    if (theme) theme.addEventListener('click', () => {
      const dark = document.documentElement.getAttribute('data-cs-theme') === 'dark';
      if (dark) document.documentElement.removeAttribute('data-cs-theme');
      else document.documentElement.setAttribute('data-cs-theme', 'dark');
      try { localStorage.setItem('cs.theme', dark ? 'light' : 'dark'); } catch (_) { /* Session only. */ }
    });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init, {once: true});
  else init();
})();
