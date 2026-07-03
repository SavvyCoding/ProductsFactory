// ── TABS ─────────────────────────────────────────────────────
const ADMIN_TABS = ['system','notif','poller','agent','pms'];

function adminTab(name, btn) {
  ADMIN_TABS.forEach(t => {
    document.getElementById('atab-' + t).classList.add('hidden');
  });
  document.querySelectorAll('.admin-tab-bar .tab-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('atab-' + name).classList.remove('hidden');
  btn.classList.add('active');
  localStorage.setItem('pf-admin-tab', name);
}

// Agent sub-tabs (Ollama / Anthropic / OpenAI / Model Selection)
const AGENT_SUBS = ['ollama','anthropic','openai','models'];
function agentSub(name, btn) {
  AGENT_SUBS.forEach(s => {
    const el = document.getElementById('asub-' + s);
    if (el) el.classList.add('hidden');
  });
  document.querySelectorAll('.agent-subtab-bar .tab-btn').forEach(b => b.classList.remove('active'));
  const panel = document.getElementById('asub-' + name);
  if (panel) panel.classList.remove('hidden');
  btn.classList.add('active');
}

// ── PASSWORD TOGGLES ─────────────────────────────────────────
function toggleField(id, btn) {
  const inp = document.getElementById(id);
  inp.type = inp.type === 'password' ? 'text' : 'password';
  btn.textContent = inp.type === 'password' ? 'Show' : 'Hide';
}

// Retired: the agent backend radios no longer toggle field visibility (the
// Agent tab uses sub-tabs now). Kept as a safe no-op for any stale callers.
function switchProfile(_p) { /* no-op */ }

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
