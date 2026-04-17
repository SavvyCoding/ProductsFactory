// ── THEME ────────────────────────────────────────────────
(function() {
  const saved = localStorage.getItem('pf-theme') || 'light';
  if (saved === 'dark') document.getElementById('html-root').setAttribute('data-theme', 'dark');
})();
function toggleDark() {
  const el = document.getElementById('html-root');
  const isDark = el.getAttribute('data-theme') === 'dark';
  el.setAttribute('data-theme', isDark ? 'light' : 'dark');
  localStorage.setItem('pf-theme', isDark ? 'light' : 'dark');
  document.getElementById('dark-btn').textContent = isDark ? '🌙' : '☀️';
}
(function() {
  const saved = localStorage.getItem('pf-theme') || 'light';
  if (saved === 'dark') document.getElementById('dark-btn').textContent = '☀️';
})();
