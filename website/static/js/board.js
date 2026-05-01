// ── GLOBAL HELPERS ────────────────────────────────────────
function escHtmlGlobal(s) {
  return String(s)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}

// ── TAB SWITCHING ─────────────────────────────────────────
const TAB_IDS = ['summary','board','sprints','prs','history','corrections','videos','live','settings'];

function switchTab(name, btn) {
  TAB_IDS.forEach(id => {
    const el = document.getElementById('tab-' + id);
    if (el) el.style.display = id === name ? '' : 'none';
  });
  document.querySelectorAll('.pf-sidebar-item').forEach(b => b.classList.remove('active'));
  if (btn) btn.classList.add('active');
  history.replaceState(null, '', `?tab=${name}`);
}

(function() {
  const params = new URLSearchParams(window.location.search);
  const tab = params.get('tab') || 'board';
  const btn = document.querySelector(`.pf-sidebar-item[onclick*="'${tab}'"]`);
  if (tab !== 'board') switchTab(tab, btn);
})();

// ── PHASE TABS ─────────────────────────────────────────────
function switchPhase(phase) {
  document.querySelectorAll('.phase-panel').forEach(p => p.style.display = 'none');
  document.querySelectorAll('.phase-tab').forEach(t => t.classList.remove('active'));
  const panel = document.getElementById('phase-' + phase);
  if (panel) panel.style.display = '';
  const btn = document.querySelector(`.phase-tab[data-phase="${phase}"]`);
  if (btn) btn.classList.add('active');
  localStorage.setItem('pf-phase', phase);
}

(function() {
  const saved = localStorage.getItem('pf-phase');
  const phases = ['Pending','Approved','Designing','Designed','Implementing','Reviewing','Reviewed','Blocked','Pushed'];
  let active = saved && phases.includes(saved) ? saved : null;
  if (!active) {
    active = phases.find(p => document.getElementById('phase-' + p)?.querySelector('.phase-card')) || 'Pending';
  }
  switchPhase(active);
})();

// ── BULK APPROVE / REJECT ────────────────────────────────
const selectedIds = new Set();

function toggleSelect(id, cb) {
  cb.checked ? selectedIds.add(id) : selectedIds.delete(id);
  updateBulkBar();
}

function selectAllPending() {
  document.querySelectorAll('.feature-checkbox').forEach(cb => {
    const id = parseInt(cb.closest('[data-feature-id]').dataset.featureId);
    cb.checked = true;
    selectedIds.add(id);
  });
  updateBulkBar();
}

function updateBulkBar() {
  const bar    = document.getElementById('bulk-bar');
  const n      = selectedIds.size;
  const ids    = [...selectedIds].join(',');
  const countEl = document.getElementById('bulk-count');
  const approveBtn = document.getElementById('bulk-approve-btn');
  const rejectBtn  = document.getElementById('bulk-reject-btn');
  if (countEl)   countEl.textContent = n > 0 ? `${n} selected` : '';
  if (approveBtn) approveBtn.disabled = n === 0;
  if (rejectBtn)  rejectBtn.disabled  = n === 0;
  const bulkIds       = document.getElementById('bulk-ids');
  const bulkRejectIds = document.getElementById('bulk-reject-ids');
  if (bulkIds)       bulkIds.value       = ids;
  if (bulkRejectIds) bulkRejectIds.value = ids;
  if (bar) bar.classList.toggle('visible', n > 0);
}

function clearSelection() {
  selectedIds.clear();
  document.querySelectorAll('.feature-checkbox').forEach(cb => cb.checked = false);
  updateBulkBar();
}

// ── FEATURE TEMPLATES ─────────────────────────────────────
function toggleTemplates() {
  const p = document.getElementById('template-panel');
  p.style.display = p.style.display === 'none' ? '' : 'none';
}

function applyTemplate(name, desc) {
  document.getElementById('feat-name').value = name;
  document.getElementById('feat-desc').value = desc;
  document.getElementById('template-panel').style.display = 'none';
  document.getElementById('feat-name').focus();
}
