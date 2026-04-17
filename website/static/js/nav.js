(function pollRunning() {
  async function update() {
    try {
      const r = await fetch('/api/products/running-count');
      const { count } = await r.json();
      const dot  = document.getElementById('nav-live-dot');
      const text = document.getElementById('nav-live-text');
      if (count > 0) {
        dot.classList.add('active');
        text.textContent = count + ' Running';
      } else {
        dot.classList.remove('active');
        text.textContent = 'Idle';
      }
    } catch(e) { /* silent */ }
  }
  update();
  setInterval(update, 30000);
})();
