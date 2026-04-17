// ── TABS ─────────────────────────────────────────────────────
const ADMIN_TABS = ['system','notif','poller','pms'];

function adminTab(name, btn) {
  ADMIN_TABS.forEach(t => {
    document.getElementById('atab-' + t).classList.add('hidden');
  });
  document.querySelectorAll('.admin-tab-bar .tab-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('atab-' + name).classList.remove('hidden');
  btn.classList.add('active');
  localStorage.setItem('pf-admin-tab', name);
}

// ── PASSWORD TOGGLES ─────────────────────────────────────────
function toggleField(id, btn) {
  const inp = document.getElementById(id);
  inp.type = inp.type === 'password' ? 'text' : 'password';
  btn.textContent = inp.type === 'password' ? 'Show' : 'Hide';
}

function switchProfile(p) {
  document.getElementById('profile-ollama').style.display = p === 'ollama' ? '' : 'none';
  document.getElementById('profile-claude').style.display = p === 'claude'  ? '' : 'none';
  document.querySelectorAll('.profile-option').forEach(el => el.classList.remove('active'));
  document.querySelector(`.profile-option input[value="${p}"]`).closest('.profile-option').classList.add('active');
}

function forceUnlockPoller() {
  fetch('/api/poller/force-unlock', {method:'POST'})
    .then(r => r.json())
    .then(d => {
      document.getElementById('unlock-msg').textContent = d.message || 'Done';
      document.getElementById('unlock-msg').style.color = 'var(--success, green)';
    })
    .catch(() => {
      document.getElementById('unlock-msg').textContent = 'Request failed';
      document.getElementById('unlock-msg').style.color = 'var(--danger, red)';
    });
}
