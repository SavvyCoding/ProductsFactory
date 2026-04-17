// ── WIZARD STATE ──────────────────────────────────────────
let wizardType = null;
let wizardStep = 1;

function openWizard() {
  wizardType = null; wizardStep = 1;
  document.querySelectorAll('.wizard-panel').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.wizard-step').forEach(s => { s.classList.remove('active','done'); });
  document.getElementById('wp-1').classList.add('active');
  document.getElementById('ws-1').classList.add('active');
  document.getElementById('step1-next').disabled = true;
  document.querySelectorAll('.type-pick-card').forEach(c => c.classList.remove('selected'));
  document.getElementById('add-product-modal').classList.add('open');
  document.body.style.overflow = 'hidden';
}
function closeWizard() {
  document.getElementById('add-product-modal').classList.remove('open');
  document.body.style.overflow = '';
}
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeWizard(); });

function selectType(type) {
  wizardType = type;
  document.querySelectorAll('.type-pick-card').forEach(c => c.classList.remove('selected'));
  document.getElementById('pick-' + type).classList.add('selected');
  document.getElementById('step1-next').disabled = false;
}

function showPanel(id) {
  document.querySelectorAll('.wizard-panel').forEach(p => p.classList.remove('active'));
  document.getElementById(id).classList.add('active');
}

function wizardNext() {
  if (!wizardType) return;
  document.getElementById('ws-1').classList.remove('active');
  document.getElementById('ws-1').classList.add('done');
  document.getElementById('ws-2').classList.add('active');
  wizardStep = 2;
  showPanel(wizardType === 'greenfield' ? 'wp-2-greenfield' : 'wp-2-brownfield');
  document.getElementById('ws-3').style.display = wizardType === 'greenfield' ? '' : 'none';
}

function wizardNext2() {
  const name = document.getElementById('gf-name').value.trim();
  const repo = document.getElementById('gf-repo').value.trim();
  if (!name) { document.getElementById('gf-name').focus(); showToast('error', 'Product name is required.'); return; }
  if (!repo)  { document.getElementById('gf-repo').focus(); showToast('error', 'GitHub repo name is required.'); return; }
  document.getElementById('ws-2').classList.remove('active');
  document.getElementById('ws-2').classList.add('done');
  document.getElementById('ws-3').classList.add('active');
  wizardStep = 3;
  showPanel('wp-3');
}

function wizardBack() {
  if (wizardStep === 2) {
    document.getElementById('ws-2').classList.remove('active','done');
    document.getElementById('ws-1').classList.remove('done');
    document.getElementById('ws-1').classList.add('active');
    wizardStep = 1;
    showPanel('wp-1');
  } else if (wizardStep === 3) {
    document.getElementById('ws-3').classList.remove('active','done');
    document.getElementById('ws-2').classList.remove('done');
    document.getElementById('ws-2').classList.add('active');
    wizardStep = 2;
    showPanel('wp-2-greenfield');
  }
}

function submitBrownfield() {
  const dir = document.getElementById('bf-working-dir').value.trim();
  if (!dir) { document.getElementById('bf-working-dir').focus(); showToast('error', 'Path is required.'); return; }
  document.getElementById('bf-h-working-dir').value = dir;
  document.getElementById('bf-hidden-form').submit();
}

function submitGreenfield() {
  const vision = document.getElementById('gf-vision').value.trim();
  if (!vision) { document.getElementById('gf-vision').focus(); showToast('error', 'Vision is required.'); return; }
  const sel   = document.getElementById('gf-stack');
  let stack = sel.value;
  if (stack === 'other') {
    stack = document.getElementById('gf-stack-other').value.trim();
    if (!stack) { showToast('error', 'Please specify the tech stack.'); return; }
  }
  document.getElementById('gf-h-name').value         = document.getElementById('gf-name').value.trim();
  document.getElementById('gf-h-repo').value         = document.getElementById('gf-repo').value.trim();
  document.getElementById('gf-h-stack').value        = stack;
  document.getElementById('gf-h-vision').value       = vision;
  document.getElementById('gf-h-suggestions').value  = document.getElementById('gf-suggestions-json').value;
  document.getElementById('gf-hidden-form').submit();
}

// ── STACK DROPDOWN ────────────────────────────────────────
function onStackChange(sel) {
  const other = document.getElementById('gf-stack-other');
  other.style.display = sel.value === 'other' ? 'block' : 'none';
}

// ── PATH PREVIEW ──────────────────────────────────────────
function updatePathPreview() {
  const repoInput  = document.getElementById('gf-repo');
  const previewEl  = document.getElementById('path-preview-text');
  const root       = document.getElementById('path-preview').dataset.root || '{root}';
  if (!repoInput || !previewEl) return;
  const repo = repoInput.value || 'my-awesome-app';
  previewEl.innerHTML = root + '/<span class="highlight">' + escHtml(repo) + '</span>';
}

// ── SEARCH & FILTER ───────────────────────────────────────
let activeFilter = 'all';

function setFilter(filter, el) {
  activeFilter = filter;
  document.querySelectorAll('.filter-chip').forEach(c => c.classList.remove('active'));
  if (el) el.classList.add('active');
  document.querySelectorAll('.stat-item[data-filter]').forEach(s => {
    s.classList.toggle('stat-active', s.dataset.filter === filter);
  });
  filterProducts();
}

function statFilter(filter, el) {
  if (activeFilter === filter) filter = 'all';
  activeFilter = filter;
  document.querySelectorAll('.stat-item[data-filter]').forEach(s => {
    s.classList.toggle('stat-active', s.dataset.filter === filter);
  });
  document.querySelectorAll('.filter-chip').forEach(c => {
    c.classList.toggle('active', c.dataset.filter === filter || (filter === 'all' && c.dataset.filter === 'all'));
  });
  filterProducts();
}

function filterProducts() {
  const q = document.getElementById('product-search') ? document.getElementById('product-search').value.toLowerCase() : '';
  const cards = document.querySelectorAll('#product-grid .product-card');
  let visible = 0;
  cards.forEach(card => {
    const matchQ = !q || (card.dataset.name || '').includes(q);
    const matchF = activeFilter === 'all'
      || card.dataset.status      === activeFilter
      || card.dataset.type        === activeFilter
      || (activeFilter === 'sprinting' && card.dataset.sprinting === 'sprinting');
    card.style.display = (matchQ && matchF) ? '' : 'none';
    if (matchQ && matchF) visible++;
  });
  const el = document.getElementById('visible-count');
  if (el) el.textContent = visible;
}

// ── RELATIVE TIME ─────────────────────────────────────────
function relTime(isoStr) {
  const diff = (Date.now() - new Date(isoStr).getTime()) / 1000;
  if (diff < 60) return 'just now';
  if (diff < 3600) return Math.floor(diff / 60) + 'm ago';
  if (diff < 86400) return Math.floor(diff / 3600) + 'h ago';
  return Math.floor(diff / 86400) + 'd ago';
}
function staleDotClass(isoStr) {
  const h = (Date.now() - new Date(isoStr).getTime()) / 3600000;
  if (h < 2) return 'stale-fresh';
  if (h < 12) return 'stale-warn';
  return 'stale-danger';
}
(function initRelTimes() {
  document.querySelectorAll('.rel-time[data-ts]').forEach(el => {
    el.textContent = relTime(el.dataset.ts);
  });
  document.querySelectorAll('[data-last-run]').forEach(card => {
    const ts = card.dataset.lastRun;
    if (!ts) return;
    const dot = card.querySelector('.staleness-dot');
    if (dot) { dot.className = 'staleness-dot ' + staleDotClass(ts); }
  });
})();

// ── VOICE INPUT ───────────────────────────────────────────
const speechSupported = 'webkitSpeechRecognition' in window || 'SpeechRecognition' in window;
if (!speechSupported) document.querySelectorAll('.voice-btn').forEach(b => b.style.display = 'none');
let activeRecognition = null;

function startVoice(targetId, btn) {
  if (!speechSupported) return;
  if (activeRecognition) { activeRecognition.stop(); activeRecognition = null; return; }
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  const rec = new SR();
  rec.lang = 'en-US'; rec.interimResults = true; rec.maxAlternatives = 1;
  activeRecognition = rec;
  btn.classList.add('recording'); btn.title = 'Click to stop';
  let final = '';
  rec.onresult = (e) => {
    let interim = '';
    for (let i = e.resultIndex; i < e.results.length; i++) {
      if (e.results[i].isFinal) final += e.results[i][0].transcript + ' ';
      else interim += e.results[i][0].transcript;
    }
    const el = document.getElementById(targetId);
    el.value = el.value.trimEnd() + (el.value ? ' ' : '') + final + interim;
  };
  rec.onend = () => {
    btn.classList.remove('recording'); btn.title = 'Voice input';
    activeRecognition = null;
    document.getElementById(targetId).value = document.getElementById(targetId).value.trim();
  };
  rec.onerror = (e) => {
    btn.classList.remove('recording'); btn.title = 'Voice input';
    activeRecognition = null;
    if (e.error !== 'no-speech' && e.error !== 'aborted') showToast('error', 'Voice error: ' + e.error);
  };
  rec.start();
}

// ── ARTICULATE VISION ─────────────────────────────────────
async function articulateVision() {
  const textarea = document.getElementById('gf-vision');
  const vision   = textarea.value.trim();
  if (vision.length < 10) { showToast('warn', 'Please enter a brief description first.'); return; }
  const sel   = document.getElementById('gf-stack');
  const stack = sel.value === 'other' ? (document.getElementById('gf-stack-other').value || 'other') : sel.value;
  const btn   = document.getElementById('articulate-btn');
  const status = document.getElementById('articulate-status');
  btn.disabled = true; btn.textContent = '⏳ Articulating…'; status.textContent = '';
  try {
    const resp = await fetch('/api/articulate/vision', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ vision, preferred_stack: stack })
    });
    if (!resp.ok) throw new Error((await resp.json()).detail || 'Server error');
    textarea.value = (await resp.json()).vision;
    status.textContent = '✓ Done';
    showToast('success', 'Vision articulated.');
  } catch (err) {
    status.textContent = '✗ ' + err.message;
    showToast('error', err.message);
  } finally {
    btn.disabled = false; btn.textContent = '✦ Articulate Vision';
  }
}

// ── AI FEATURE SUGGESTIONS ────────────────────────────────
let selectedSuggestions = [];

async function suggestFeatures() {
  const vision = document.getElementById('gf-vision').value.trim();
  const sel    = document.getElementById('gf-stack');
  const stack  = sel.value === 'other' ? (document.getElementById('gf-stack-other').value || 'other') : sel.value;
  if (vision.length < 20) { showToast('warn', 'Please describe the product vision first.'); return; }
  const btn    = document.getElementById('suggest-btn');
  const panel  = document.getElementById('gf-suggestions');
  const grid   = document.getElementById('suggestions-grid');
  const hint   = document.getElementById('suggestions-hint');
  const status = document.getElementById('suggest-status');
  btn.disabled = true; btn.textContent = '⏳ Generating backlog…'; status.textContent = 'This takes ~15s for a full backlog...';
  selectedSuggestions = []; updateSuggestionsJson();
  try {
    const resp = await fetch('/api/recommend/features', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ vision, preferred_stack: stack })
    });
    if (!resp.ok) throw new Error(resp.status === 501 ? 'ANTHROPIC_API_KEY not configured' : 'Server error');
    const data = await resp.json();
    panel.classList.remove('hidden');
    selectedSuggestions = [...data.features];
    updateSuggestionsJson();
    grid.innerHTML = data.features.map((f, i) => `
      <div class="suggestion-card selected" id="sc-${i}" onclick="toggleSuggestion(${i}, ${escAttr(JSON.stringify(f))})">
        <div class="suggestion-name">${escHtml(f.name)}</div>
        <div class="suggestion-desc">${escHtml(f.description || '')}</div>
        <span class="suggestion-tag">✓ Selected</span>
      </div>`).join('');
    hint.textContent = `${data.features.length} features — all selected. Click any to deselect.`;
    showToast('success', `Full backlog ready: ${data.features.length} features.`);
  } catch (err) {
    panel.classList.remove('hidden');
    grid.innerHTML = `<div class="notice notice-amber" style="grid-column:1/-1">${escHtml(err.message)}</div>`;
    hint.textContent = '';
    showToast('error', err.message);
  } finally {
    btn.disabled = false; btn.textContent = '📋 Generate Full Backlog'; status.textContent = '';
  }
}

function toggleSuggestion(idx, feature) {
  const card = document.getElementById('sc-' + idx);
  const pos  = selectedSuggestions.findIndex(f => f.name === feature.name);
  if (pos >= 0) {
    selectedSuggestions.splice(pos, 1);
    card.classList.remove('selected');
    card.querySelector('.suggestion-tag').textContent = 'Click to select';
  } else {
    selectedSuggestions.push(feature);
    card.classList.add('selected');
    card.querySelector('.suggestion-tag').textContent = '✓ Selected';
  }
  updateSuggestionsJson();
  document.getElementById('suggest-status').textContent =
    selectedSuggestions.length ? `${selectedSuggestions.length} selected` : '';
}

function updateSuggestionsJson() {
  document.getElementById('gf-suggestions-json').value = JSON.stringify(selectedSuggestions);
}

// ── UTILITIES ─────────────────────────────────────────────
function escHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
function escAttr(s) {
  return "'" + String(s).replace(/'/g, "\\'") + "'";
}

// Handle ?filter= query param
(function() {
  const params = new URLSearchParams(window.location.search);
  const f = params.get('filter');
  if (f) {
    const chip = document.querySelector(`.filter-chip[data-filter="${f}"]`);
    setFilter(f, chip);
  }
})();
