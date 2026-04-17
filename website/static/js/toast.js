function showToast(type, message, duration) {
  duration = duration || 4000;
  const container = document.getElementById('toast-container');
  const toast = document.createElement('div');
  toast.className = 'toast toast-' + type;
  toast.innerHTML = `
    <span class="toast-msg">${escHtmlGlobal(message)}</span>
    <div class="toast-bar"></div>
    <button class="toast-close" onclick="this.parentElement.remove()" aria-label="Dismiss">✕</button>
  `;
  container.appendChild(toast);
  requestAnimationFrame(() => {
    requestAnimationFrame(() => toast.classList.add('toast-in'));
  });
  const bar = toast.querySelector('.toast-bar');
  bar.style.transition = `width ${duration}ms linear`;
  requestAnimationFrame(() => {
    requestAnimationFrame(() => { bar.style.width = '0%'; });
  });
  const timer = setTimeout(() => {
    toast.classList.remove('toast-in');
    toast.addEventListener('transitionend', () => toast.remove(), { once: true });
  }, duration);
  toast.querySelector('.toast-close').addEventListener('click', () => clearTimeout(timer));
}

function escHtmlGlobal(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
