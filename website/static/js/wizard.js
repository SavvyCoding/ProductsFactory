// ── WIZARD STATE ────────────────────────────────────────────────────────────
// New flow:  Type → Vision → Stack → (UI Template if web) → Details
// Brownfield: Type → Path
let wizardType   = null;
let wizardStep   = 1;          // current step number
let catalog      = null;       // {stacks, databases, ui_templates} from /api/wizard/catalog
let selectedStack    = null;   // stack id (e.g. "python_fastapi")
let selectedDb       = "postgresql";
let selectedUITemplate = "agent_choose";
let stackRecommendation = null;  // {recommended, alternatives, database, reasoning}
let uiRecommendation    = null;  // {recommended, reasoning}

// ── BASIC OPEN/CLOSE ────────────────────────────────────────────────────────
function openWizard() {
  wizardType = null;
  wizardStep = 1;
  selectedStack = null;
  selectedDb = "postgresql";
  selectedUITemplate = "agent_choose";
  stackRecommendation = null;
  uiRecommendation = null;
  selectedSuggestions = [];
  resetStepIndicators();
  hideAllPanels();
  document.getElementById('wp-1').classList.add('active');
  document.getElementById('ws-1').classList.add('active');
  document.getElementById('step1-next').disabled = true;
  document.querySelectorAll('.type-pick-card').forEach(c => c.classList.remove('selected'));
  document.getElementById('add-product-modal').classList.add('open');
  document.body.style.overflow = 'hidden';
  // Pre-fetch catalogue so the Stack/UI steps render instantly when reached.
  if (!catalog) loadCatalog();
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

function hideAllPanels() {
  document.querySelectorAll('.wizard-panel').forEach(p => p.classList.remove('active'));
}
function showPanel(id) {
  hideAllPanels();
  document.getElementById(id).classList.add('active');
}
function resetStepIndicators() {
  document.querySelectorAll('.wizard-step').forEach(s => {
    s.classList.remove('active', 'done', 'hidden');
  });
}
function setStepIndicator(activeIdx) {
  // Mark steps 1..activeIdx-1 as 'done', activeIdx as 'active', rest plain.
  for (let i = 1; i <= 6; i++) {
    const el = document.getElementById('ws-' + i);
    if (!el) continue;
    el.classList.remove('active', 'done');
    if (i < activeIdx)      el.classList.add('done');
    else if (i === activeIdx) el.classList.add('active');
  }
}
function setUIStepVisible(visible) {
  // Step 4 is conditional on the selected stack having a web component.
  const ws4 = document.getElementById('ws-4');
  if (!ws4) return;
  ws4.classList.toggle('hidden', !visible);
}

// ── STEP 1 → 2 ──────────────────────────────────────────────────────────────
function wizardNext() {
  if (!wizardType) return;
  if (wizardType === 'brownfield') {
    // Brownfield: skip ahead — only Type → Path. Hide the step indicators
    // we won't use so the visual count matches the actual flow.
    ['ws-2','ws-3','ws-4','ws-5','ws-6'].forEach(id => document.getElementById(id).classList.add('hidden'));
    showPanel('wp-2-brownfield');
    return;
  }
  // Greenfield: go to Vision step
  setStepIndicator(2);
  setUIStepVisible(true);  // visible by default until a non-web stack is picked
  wizardStep = 2;
  showPanel('wp-2-greenfield');
}

// ── STEP 2 → 3 (greenfield) ─────────────────────────────────────────────────
async function wizardToStack() {
  const vision = document.getElementById('gf-vision').value.trim();
  if (vision.length < 20) {
    showToast('warn', 'Please write at least a couple of sentences about your product before we can suggest a stack.');
    document.getElementById('gf-vision').focus();
    return;
  }
  setStepIndicator(3);
  wizardStep = 3;
  showPanel('wp-3-stack');
  // Render the picker from the cached catalogue (fetch first if needed)
  if (!catalog) await loadCatalog();
  renderStackPicker();
  renderDbPicker();
  // Fire AI recommendation in the background. Vision-driven; non-blocking.
  fetchStackRecommendation(vision);
}

// ── STEP 3 → 4 or 5 (greenfield) ────────────────────────────────────────────
async function wizardToUI() {
  if (!selectedStack) {
    showToast('warn', 'Pick a stack first.');
    return;
  }
  const stackOpt = catalog.stacks.flatMap(g => g.options).find(o => o.id === selectedStack);
  const isWeb = !!(stackOpt && stackOpt.has_web_ui);
  setUIStepVisible(isWeb);
  if (!isWeb) {
    // Skip UI step entirely — go straight to backlog generator
    selectedUITemplate = "";
    wizardToBacklog();
    return;
  }
  setStepIndicator(4);
  wizardStep = 4;
  showPanel('wp-4-ui');
  renderUITemplateGallery();
  const vision = document.getElementById('gf-vision').value.trim();
  fetchUIRecommendation(vision, selectedStack);
}

// ── STEP 4 (or skipped) → 5 BACKLOG (greenfield) ─────────────────────────────
function wizardToBacklog() {
  if (!selectedUITemplate) selectedUITemplate = "agent_choose";
  setStepIndicator(5);
  wizardStep = 5;
  showPanel('wp-5-backlog');
}

// ── STEP 5 → 6 (greenfield) ─────────────────────────────────────────────────
function wizardToDetailsFromBacklog() {
  setStepIndicator(6);
  wizardStep = 6;
  showPanel('wp-6-details');
}

// ── BACK (greenfield + brownfield) ──────────────────────────────────────────
function wizardBack() {
  // Brownfield: only Type → Path, so back from path goes to step 1.
  if (wizardType === 'brownfield') {
    showPanel('wp-1');
    ['ws-2','ws-3','ws-4','ws-5','ws-6'].forEach(id => document.getElementById(id).classList.remove('hidden'));
    return;
  }
  // Greenfield: walk backwards through the steps.
  if (wizardStep === 6) {
    // Back from Details → Backlog
    setStepIndicator(5);
    wizardStep = 5;
    showPanel('wp-5-backlog');
  } else if (wizardStep === 5) {
    // Back from Backlog → UI (if web) or Stack
    const stackOpt = catalog && catalog.stacks.flatMap(g => g.options).find(o => o.id === selectedStack);
    const isWeb = !!(stackOpt && stackOpt.has_web_ui);
    if (isWeb) { setStepIndicator(4); wizardStep = 4; showPanel('wp-4-ui'); }
    else       { setStepIndicator(3); wizardStep = 3; showPanel('wp-3-stack'); }
  } else if (wizardStep === 4) {
    setStepIndicator(3);
    wizardStep = 3;
    showPanel('wp-3-stack');
  } else if (wizardStep === 3) {
    setStepIndicator(2);
    wizardStep = 2;
    showPanel('wp-2-greenfield');
  } else if (wizardStep === 2) {
    setStepIndicator(1);
    wizardStep = 1;
    showPanel('wp-1');
  }
}

// ── CATALOGUE LOADER ────────────────────────────────────────────────────────
async function loadCatalog() {
  try {
    const resp = await fetch('/api/wizard/catalog');
    if (!resp.ok) throw new Error('Could not load wizard catalog');
    catalog = await resp.json();
  } catch (err) {
    showToast('error', err.message);
    catalog = { stacks: [], databases: [], ui_templates: [] };
  }
}

// ── STACK PICKER RENDER ─────────────────────────────────────────────────────
function renderStackPicker() {
  const root = document.getElementById('stack-picker');
  if (!catalog || !catalog.stacks.length) {
    root.innerHTML = '<div class="muted">No stacks configured.</div>';
    return;
  }
  root.innerHTML = catalog.stacks.map(group => `
    <div class="stack-group">
      <div class="stack-group-label">${escHtml(group.label)}</div>
      <div class="stack-options-grid">
        ${group.options.map(opt => `
          <div class="stack-option ${selectedStack === opt.id ? 'selected' : ''} ${stackRecommendation && stackRecommendation.recommended === opt.id ? 'recommended-badge' : ''}"
               data-stack-id="${escAttrSafe(opt.id)}"
               onclick="selectStack(${escAttr(opt.id)})">
            <div class="stack-option-label">${escHtml(opt.label)}</div>
            <div class="stack-option-desc">${escHtml(opt.description)}</div>
          </div>`).join('')}
      </div>
    </div>`).join('');
}

function renderDbPicker() {
  const root = document.getElementById('db-picker');
  if (!catalog || !catalog.databases.length) {
    root.innerHTML = '<div class="muted">No database options configured.</div>';
    return;
  }
  root.innerHTML = catalog.databases.map(opt => `
    <div class="db-option ${selectedDb === opt.id ? 'selected' : ''}"
         data-db-id="${escAttrSafe(opt.id)}"
         title="${escAttrSafe(opt.description)}"
         onclick="selectDb(${escAttr(opt.id)})">
      ${escHtml(opt.label)}
    </div>`).join('');
}

function selectStack(stackId) {
  selectedStack = stackId;
  document.querySelectorAll('.stack-option').forEach(el => {
    el.classList.toggle('selected', el.dataset.stackId === stackId);
  });
}

function selectDb(dbId) {
  selectedDb = dbId;
  document.querySelectorAll('.db-option').forEach(el => {
    el.classList.toggle('selected', el.dataset.dbId === dbId);
  });
}

// ── STACK AI RECOMMENDATION ─────────────────────────────────────────────────
async function fetchStackRecommendation(vision) {
  const card = document.getElementById('stack-recommendation');
  card.innerHTML = `
    <div class="recommendation-label">Asking AI for a recommendation…</div>
    <div class="recommendation-reason muted">This usually takes 5–10 seconds.</div>
  `;
  try {
    const resp = await fetch('/api/wizard/recommend-stack', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      credentials: 'same-origin',
      body: JSON.stringify({ vision }),
    });
    if (!resp.ok) throw new Error((await resp.json()).detail || 'AI error');
    stackRecommendation = await resp.json();
    renderStackRecommendation();
    // Auto-select the recommended stack + database; user can override.
    if (!selectedStack) selectStack(stackRecommendation.recommended);
    if (stackRecommendation.database) selectDb(stackRecommendation.database);
    renderStackPicker();   // re-render so the recommended-badge appears
  } catch (err) {
    card.innerHTML = `<div class="recommendation-label">Recommendation unavailable</div>
      <div class="recommendation-reason muted">${escHtml(err.message)} — pick a stack from the list below.</div>`;
  }
}

function renderStackRecommendation() {
  const card = document.getElementById('stack-recommendation');
  if (!stackRecommendation || !catalog) return;
  const opt = catalog.stacks.flatMap(g => g.options).find(o => o.id === stackRecommendation.recommended);
  if (!opt) { card.innerHTML = ''; return; }
  const dbOpt = catalog.databases.find(d => d.id === stackRecommendation.database);
  card.innerHTML = `
    <div class="recommendation-label">★ Recommended for your vision</div>
    <div class="recommendation-title">${escHtml(opt.label)}${dbOpt ? ' + ' + escHtml(dbOpt.label) : ''}</div>
    <div class="recommendation-reason">${escHtml(stackRecommendation.reasoning || '')}</div>
    <div class="recommendation-actions">
      <button class="pf-btn pf-btn--primary" onclick="selectStack(${escAttr(opt.id)}); ${dbOpt ? 'selectDb(' + escAttr(dbOpt.id) + ');' : ''} showToast('success', 'Recommendation applied.')">Use this</button>
      <button class="pf-btn pf-btn--secondary" onclick="document.getElementById('stack-picker').scrollIntoView({behavior:'smooth'})">Browse all options</button>
    </div>
  `;
}

// ── UI TEMPLATE GALLERY ─────────────────────────────────────────────────────
function renderUITemplateGallery() {
  const root = document.getElementById('ui-template-gallery');
  if (!catalog || !catalog.ui_templates.length) {
    root.innerHTML = '<div class="muted">No UI templates configured.</div>';
    return;
  }
  root.innerHTML = catalog.ui_templates.map(tpl => `
    <div class="ui-template-card ${selectedUITemplate === tpl.id ? 'selected' : ''} ${uiRecommendation && uiRecommendation.recommended === tpl.id ? 'recommended-badge' : ''}"
         data-ui-id="${escAttrSafe(tpl.id)}"
         onclick="selectUITemplate(${escAttr(tpl.id)})">
      <div class="ui-template-name">${escHtml(tpl.label)}</div>
      <div class="ui-template-desc">${escHtml(tpl.description)}</div>
    </div>`).join('');
}

function selectUITemplate(tplId) {
  selectedUITemplate = tplId;
  document.querySelectorAll('.ui-template-card').forEach(el => {
    el.classList.toggle('selected', el.dataset.uiId === tplId);
  });
}

async function fetchUIRecommendation(vision, stackId) {
  const card = document.getElementById('ui-recommendation');
  card.innerHTML = `
    <div class="recommendation-label">Asking AI for a UI recommendation…</div>
    <div class="recommendation-reason muted">A few seconds.</div>
  `;
  try {
    const resp = await fetch('/api/wizard/recommend-ui-template', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      credentials: 'same-origin',
      body: JSON.stringify({ vision, stack_id: stackId }),
    });
    if (!resp.ok) throw new Error((await resp.json()).detail || 'AI error');
    uiRecommendation = await resp.json();
    const tpl = catalog.ui_templates.find(t => t.id === uiRecommendation.recommended);
    if (!tpl) { card.innerHTML = ''; return; }
    card.innerHTML = `
      <div class="recommendation-label">★ Recommended UI</div>
      <div class="recommendation-title">${escHtml(tpl.label)}</div>
      <div class="recommendation-reason">${escHtml(uiRecommendation.reasoning || '')}</div>
      <div class="recommendation-actions">
        <button class="pf-btn pf-btn--primary" onclick="selectUITemplate(${escAttr(tpl.id)}); showToast('success', 'Recommendation applied.')">Use this</button>
      </div>`;
    if (selectedUITemplate === 'agent_choose') selectUITemplate(tpl.id);
    renderUITemplateGallery();
  } catch (err) {
    card.innerHTML = `<div class="recommendation-label">Recommendation unavailable</div>
      <div class="recommendation-reason muted">${escHtml(err.message)} — pick one from the gallery.</div>`;
  }
}

// ── PATH PREVIEW ────────────────────────────────────────────────────────────
function updatePathPreview() {
  const repoInput = document.getElementById('gf-repo');
  const previewEl = document.getElementById('path-preview-text');
  const root      = document.getElementById('path-preview').dataset.root || '{root}';
  if (!repoInput || !previewEl) return;
  const repo = repoInput.value || 'my-awesome-app';
  previewEl.innerHTML = root + '/<span class="highlight">' + escHtml(repo) + '</span>';
}

// ── SUBMISSIONS ─────────────────────────────────────────────────────────────
function submitBrownfield() {
  const dir = document.getElementById('bf-working-dir').value.trim();
  if (!dir) {
    document.getElementById('bf-working-dir').focus();
    showToast('error', 'Path is required.');
    return;
  }
  document.getElementById('bf-h-working-dir').value = dir;
  document.getElementById('bf-hidden-form').submit();
}

function submitGreenfield() {
  const name   = document.getElementById('gf-name').value.trim();
  const repo   = document.getElementById('gf-repo').value.trim();
  const vision = document.getElementById('gf-vision').value.trim();
  if (!name)   { showToast('error', 'Product name is required.');     document.getElementById('gf-name').focus(); return; }
  if (!repo)   { showToast('error', 'GitHub repo name is required.'); document.getElementById('gf-repo').focus(); return; }
  if (!vision) { showToast('error', 'Vision is required.');           wizardStep = 2; showPanel('wp-2-greenfield'); return; }
  if (!selectedStack) { showToast('error', 'Pick a stack first.');    wizardStep = 3; showPanel('wp-3-stack'); return; }
  document.getElementById('gf-h-name').value         = name;
  document.getElementById('gf-h-repo').value         = repo;
  document.getElementById('gf-h-stack').value        = selectedStack;
  document.getElementById('gf-h-database').value     = selectedDb || '';
  document.getElementById('gf-h-ui-template').value  = selectedUITemplate || '';
  document.getElementById('gf-h-vision').value       = vision;
  document.getElementById('gf-h-suggestions').value  = document.getElementById('gf-suggestions-json').value;
  document.getElementById('gf-hidden-form').submit();
}

// ── ARTICULATE VISION ───────────────────────────────────────────────────────
async function articulateVision() {
  const textarea = document.getElementById('gf-vision');
  const vision   = textarea.value.trim();
  if (vision.length < 10) { showToast('warn', 'Please enter a brief description first.'); return; }
  const btn      = document.getElementById('articulate-btn');
  const status   = document.getElementById('articulate-status');
  btn.disabled = true; btn.textContent = '⏳ Articulating…'; status.textContent = '';
  try {
    const resp = await fetch('/api/articulate/vision', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      credentials: 'same-origin',
      body: JSON.stringify({ vision, preferred_stack: selectedStack || 'unspecified' }),
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

// ── AI FEATURE SUGGESTIONS ──────────────────────────────────────────────────
let selectedSuggestions = [];
// Full backlog returned by /api/recommend/features for the current vision,
// kept by index so suggestion-card onclicks can pass `idx` instead of
// embedding the whole feature object in an HTML attribute (the inline JSON
// breaks attribute quoting — see wizard `selectStack` for the same fix).
let suggestedFeatures = [];

async function suggestFeatures() {
  const vision = document.getElementById('gf-vision').value.trim();
  if (vision.length < 20) { showToast('warn', 'Vision is too short to generate a backlog.'); return; }
  if (!selectedStack)     { showToast('warn', 'Pick a stack first (step 3).'); return; }
  const btn    = document.getElementById('suggest-btn');
  const panel  = document.getElementById('gf-suggestions');
  const grid   = document.getElementById('suggestions-grid');
  const hint   = document.getElementById('suggestions-hint');
  const status = document.getElementById('suggest-status');
  btn.disabled = true; btn.textContent = '⏳ Generating backlog…'; status.textContent = 'This takes ~15s for a full backlog…';
  selectedSuggestions = []; updateSuggestionsJson();
  try {
    const resp = await fetch('/api/recommend/features', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      credentials: 'same-origin',
      body: JSON.stringify({ vision, preferred_stack: selectedStack }),
    });
    if (!resp.ok) throw new Error(resp.status === 501 ? 'ANTHROPIC_API_KEY not configured' : 'Server error');
    const data = await resp.json();
    panel.classList.remove('hidden');
    suggestedFeatures = data.features;
    selectedSuggestions = [...data.features];
    updateSuggestionsJson();
    grid.innerHTML = data.features.map((f, i) => `
      <div class="suggestion-card selected" id="sc-${i}" onclick="toggleSuggestion(${i})">
        <button type="button" class="suggestion-edit-btn" title="Edit feature"
                aria-label="Edit feature"
                onclick="event.stopPropagation(); openSuggestionEditor(${i})">
          <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 1 1 3 3L7 19l-4 1 1-4z"/></svg>
        </button>
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
    btn.disabled = false; btn.textContent = '📋 Generate Full Backlog (optional)'; status.textContent = '';
  }
}

function toggleSuggestion(idx) {
  const feature = suggestedFeatures[idx];
  if (!feature) return;
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

// ── PER-FEATURE EDITOR (backlog step) ──────────────────────────────────────
// Card edit button → modal → save (in-place update of suggestedFeatures +
// selectedSuggestions) OR remove (deselect from backlog). Keyed by `idx`
// into suggestedFeatures so we keep all the mutation logic in one place.
function openSuggestionEditor(idx) {
  const f = suggestedFeatures[idx];
  if (!f) return;
  document.getElementById('se-idx').value      = idx;
  document.getElementById('se-name').value     = f.name || '';
  document.getElementById('se-desc').value     = f.description || '';
  document.getElementById('se-type').value     = f.feature_type || 'feature';
  document.getElementById('se-priority').value = (f.priority != null) ? f.priority : 50;
  document.getElementById('suggestion-editor').classList.add('open');
  document.getElementById('se-name').focus();
}

function closeSuggestionEditor() {
  document.getElementById('suggestion-editor').classList.remove('open');
}

function saveSuggestionEditor() {
  const idx = parseInt(document.getElementById('se-idx').value, 10);
  const f   = suggestedFeatures[idx];
  if (!f) { closeSuggestionEditor(); return; }
  const name = document.getElementById('se-name').value.trim();
  if (!name) { showToast('warn', 'Name is required.'); return; }
  const desc = document.getElementById('se-desc').value.trim();
  const type = document.getElementById('se-type').value;
  let prio   = parseInt(document.getElementById('se-priority').value, 10);
  if (isNaN(prio) || prio < 1)   prio = 1;
  if (prio > 100)                prio = 100;

  // Update master list AND the selectedSuggestions entry in place so
  // findIndex-by-name in toggleSuggestion keeps matching after a rename.
  const oldName = f.name;
  f.name = name; f.description = desc; f.feature_type = type; f.priority = prio;
  const selPos = selectedSuggestions.findIndex(s => s.name === oldName);
  if (selPos >= 0) selectedSuggestions[selPos] = f;
  updateSuggestionsJson();

  // Re-render the card content (preserve selected/edit-button structure).
  const card = document.getElementById('sc-' + idx);
  if (card) {
    card.querySelector('.suggestion-name').textContent = name;
    card.querySelector('.suggestion-desc').textContent = desc;
  }
  closeSuggestionEditor();
  showToast('success', 'Feature updated.');
}

function removeSuggestionFromEditor() {
  const idx = parseInt(document.getElementById('se-idx').value, 10);
  const f   = suggestedFeatures[idx];
  if (!f) { closeSuggestionEditor(); return; }
  const selPos = selectedSuggestions.findIndex(s => s.name === f.name);
  if (selPos >= 0) {
    selectedSuggestions.splice(selPos, 1);
    updateSuggestionsJson();
  }
  // Mark card as deselected (mirrors toggleSuggestion's deselect branch).
  const card = document.getElementById('sc-' + idx);
  if (card) {
    card.classList.remove('selected');
    const tag = card.querySelector('.suggestion-tag');
    if (tag) tag.textContent = 'Click to select';
  }
  document.getElementById('suggest-status').textContent =
    selectedSuggestions.length ? `${selectedSuggestions.length} selected` : '';
  closeSuggestionEditor();
  showToast('info', `Removed "${f.name}" from backlog.`);
}

// ── SEARCH & FILTER (unchanged) ─────────────────────────────────────────────
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

// ── RELATIVE TIME (unchanged) ───────────────────────────────────────────────
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

// ── VOICE INPUT (unchanged) ─────────────────────────────────────────────────
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

// ── UTILITIES ───────────────────────────────────────────────────────────────
function escHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
function escAttr(s) {
  // Inline-event embedding helper (wraps in single quotes; escapes inner singles).
  return "'" + String(s).replace(/'/g, "\\'") + "'";
}
function escAttrSafe(s) {
  // Plain HTML-attribute string (no surrounding quotes).
  return String(s).replace(/&/g,'&amp;').replace(/"/g,'&quot;');
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
