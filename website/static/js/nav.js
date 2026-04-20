(function pollRunning() {
  async function update() {
    try {
      const r = await fetch('/api/products/running-count');
      const { count } = await r.json();
      const link = document.getElementById('nav-live');
      const dot  = document.getElementById('nav-live-dot');
      const text = document.getElementById('nav-live-text');
      if (count > 0) {
        if (link) link.classList.add('is-running');
        if (dot)  dot.classList.add('active');
        if (text) text.textContent = count + ' Running';
      } else {
        if (link) link.classList.remove('is-running');
        if (dot)  dot.classList.remove('active');
        if (text) text.textContent = 'Idle';
      }
    } catch(e) { /* silent */ }
  }
  update();
  setInterval(update, 30000);
})();
