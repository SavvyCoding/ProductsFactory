// ── Feature Detail Slide-Out Panel ────────────────────────
// Opens when a feature card/row is clicked
// Fetches data from multiple API endpoints in parallel

let _panelOpen = false;
let _panelFeatureId = null;

function openFeaturePanel(featureId) {
  _panelFeatureId = featureId;
  const backdrop = document.getElementById('feature-panel-backdrop');
  const panel = document.getElementById('feature-panel');
  if (!backdrop || !panel) return;

  backdrop.classList.add('open');
  panel.classList.add('open');
  _panelOpen = true;
  document.body.style.overflow = 'hidden';

  // Show loading state
  document.getElementById('fp-body').innerHTML = '<div style="text-align:center;padding:40px;color:var(--color-fg-muted)">Loading...</div>';

  // Fetch all data in parallel
  Promise.all([
    fetch(`/api/features/${featureId}`).then(r => r.json()),
    fetch(`/api/features/${featureId}/comments`).then(r => r.json()).catch(() => []),
    fetch(`/api/features/${featureId}/changelog`).then(r => r.json()).catch(() => []),
    fetch(`/api/features/${featureId}/labels`).then(r => r.json()).catch(() => []),
    fetch(`/api/features/${featureId}/links`).then(r => r.json()).catch(() => []),
    fetch(`/api/features/${featureId}/story`).then(r => r.json()).catch(() => ({source: null, html: ''})),
  ]).then(([feature, comments, changelog, labels, links, story]) => {
    renderFeaturePanel(feature, comments, changelog, labels, links, story);
  }).catch(err => {
    document.getElementById('fp-body').innerHTML =
      `<div style="color:var(--color-danger-fg);padding:20px">Failed to load feature: ${escHtmlGlobal(err.message)}</div>`;
  });
}

function closeFeaturePanel() {
  const backdrop = document.getElementById('feature-panel-backdrop');
  const panel = document.getElementById('feature-panel');
  if (backdrop) backdrop.classList.remove('open');
  if (panel) panel.classList.remove('open');
  _panelOpen = false;
  _panelFeatureId = null;
  document.body.style.overflow = '';
}

// Close on Escape
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && _panelOpen) closeFeaturePanel();
});

function renderFeaturePanel(f, comments, changelog, labels, links, story) {
  const body = document.getElementById('fp-body');
  if (!body) return;

  // Header
  const headerEl = document.getElementById('fp-title');
  if (headerEl) headerEl.textContent = f.name;

  const statusColors = {
    Pending: 'muted', Approved: 'success', Designing: 'purple', Designed: 'purple',
    Implementing: 'info', Reviewing: 'warning', Reviewed: 'warning',
    Blocked: 'danger', Pushed: 'success', Rejected: 'danger', Deferred: 'muted'
  };

  // Build body HTML
  let html = '';

  // Status + type row
  html += `<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:16px">
    <span class="badge badge-status-${f.status.toLowerCase()}">${escHtmlGlobal(f.status)}</span>
    ${f.feature_type !== 'feature' ? `<span class="type-badge type-${f.feature_type}">${f.feature_type}</span>` : ''}
    ${f.source === 'ai' ? '<span class="ai-badge">AI</span>' : ''}
    <span style="font-size:12px;color:var(--color-fg-muted);margin-left:auto">ID: ${f.id}</span>
  </div>`;

  // Meta grid
  html += `<div class="pf-panel__section">
    <dl class="pf-panel__meta">
      <dt>Priority</dt><dd>${f.priority || '—'}</dd>
      <dt>Created</dt><dd>${f.created_at ? f.created_at.replace('T',' ').slice(0,16) : '—'}</dd>
      ${f.due_date ? `<dt>Due date</dt><dd>${f.due_date}</dd>` : ''}
      ${f.story_points != null ? `<dt>Story points</dt><dd>${f.story_points}</dd>` : ''}
      ${f.pr_url ? `<dt>PR</dt><dd><a href="${f.pr_url}" target="_blank" style="color:var(--color-accent-fg)">#${f.pr_number} ↗</a></dd>` : ''}
      ${f.sprint_id ? `<dt>Feature</dt><dd>Feature #${f.sprint_id}</dd>` : ''}
    </dl>
  </div>`;

  // Labels
  html += `<div class="pf-panel__section">
    <div class="pf-panel__section-title">Labels</div>
    <div style="display:flex;flex-wrap:wrap;gap:4px">`;
  if (labels.length) {
    labels.forEach(l => {
      html += `<span class="pf-label-pill" style="background:${l.color}22;border:1px solid ${l.color}55;color:${l.color}">${escHtmlGlobal(l.name)}
        <button onclick="removeLabel(${f.id},${l.id})" style="background:none;border:none;cursor:pointer;color:inherit;font-size:10px;padding:0 0 0 4px" title="Remove">×</button>
      </span>`;
    });
  }
  html += `<button onclick="showAddLabel(${f.id})" class="pf-label-add" style="font-size:11px;padding:2px 8px;border:1px dashed var(--color-border-default);border-radius:var(--radius-full);background:none;cursor:pointer;color:var(--color-fg-muted)">+ Add</button>
    </div>
    <div id="fp-add-label" style="display:none;margin-top:8px">
      <select id="fp-label-select" style="font-size:12px;padding:4px 8px;border:1px solid var(--color-border-default);border-radius:var(--radius-md)">
        <option value="">Select label...</option>
      </select>
      <button onclick="addSelectedLabel(${f.id})" class="btn btn-sm btn-green" style="margin-left:4px">Add</button>
    </div>
  </div>`;

  // Links
  html += `<div class="pf-panel__section">
    <div class="pf-panel__section-title">Links</div>`;
  if (links.length) {
    links.forEach(l => {
      const targetName = l.source_id === f.id ? (l.target_name || `Feature #${l.target_id}`) : (l.source_name || `Feature #${l.source_id}`);
      const linkType = l.link_type.replace(/_/g, ' ');
      html += `<div class="pf-link-item">
        <span class="pf-link-type">${escHtmlGlobal(linkType)}</span>
        <span>${escHtmlGlobal(targetName)}</span>
        <button onclick="removeLink(${f.id},${l.id})" style="background:none;border:none;cursor:pointer;color:var(--color-fg-muted);font-size:11px;margin-left:auto" title="Remove">×</button>
      </div>`;
    });
  } else {
    html += '<div style="font-size:12px;color:var(--color-fg-muted)">No links</div>';
  }
  html += '</div>';

  // Description
  if (f.description) {
    html += `<div class="pf-panel__section">
      <div class="pf-panel__section-title">Description</div>
      <div style="font-size:13px;line-height:1.6;color:var(--color-fg-default);white-space:pre-wrap">${escHtmlGlobal(f.description)}</div>
    </div>`;
  }

  // Story (rendered markdown from docs/story_<NNN>.md or design_doc fallback)
  if (story && story.html) {
    const sourceLabel = ({
      'story_file': 'Story doc',
      'design_doc_path': 'Design doc',
      'design_doc': 'Design (inline)',
    })[story.source] || 'Story';
    const pathSuffix = story.path
      ? ` <span style="font-weight:400;color:var(--color-fg-muted);font-size:11px">${escHtmlGlobal(story.path.split('/').slice(-2).join('/'))}</span>`
      : '';
    html += `<div class="pf-panel__section">
      <div class="pf-panel__section-title">${sourceLabel}${pathSuffix}</div>
      <div class="pf-story">${story.html}</div>
    </div>`;
  }

  // Blocked reason — render only when the feature is actually Blocked.
  // The `blocked_reason` column lingers as historical text after a feature
  // transitions out of Blocked (e.g. PM rolls it back to Approved); showing
  // it unconditionally creates a "this feature looks Blocked" false-positive
  // in the UI. Gating on status mirrors the new product.html phase listing.
  if (f.blocked_reason && f.status === 'Blocked') {
    html += `<div class="pf-panel__section">
      <div style="padding:8px 12px;background:var(--color-danger-subtle);border:1px solid var(--color-danger-fg);border-radius:var(--radius-md);font-size:13px;color:var(--color-danger-fg)">
        <strong>Blocked:</strong> ${escHtmlGlobal(f.blocked_reason)}
      </div>
    </div>`;
  }

  // Comments
  html += `<div class="pf-panel__section">
    <div class="pf-panel__section-title">Comments (${comments.length})</div>`;
  if (comments.length) {
    comments.forEach(c => {
      html += `<div class="pf-comment">
        <div class="pf-comment__header">
          <span class="pf-comment__author">${escHtmlGlobal(c.author)}</span>
          <span>${c.created_at ? c.created_at.replace('T',' ').slice(0,16) : ''}</span>
        </div>
        <div class="pf-comment__body">${escHtmlGlobal(c.body)}</div>
      </div>`;
    });
  }
  html += `<div class="pf-comment-input">
      <textarea id="fp-comment-text" placeholder="Add a comment..." rows="2" style="width:100%;padding:8px;border:1px solid var(--color-border-default);border-radius:var(--radius-md);font-size:13px;font-family:inherit;resize:vertical"></textarea>
      <button onclick="addComment(${f.id})" class="btn btn-sm" style="margin-top:4px">Comment</button>
    </div>
  </div>`;

  // Changelog
  html += `<div class="pf-panel__section">
    <details>
      <summary style="font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:0.5px;color:var(--color-fg-subtle);cursor:pointer;user-select:none">
        Changelog (${changelog.length})
      </summary>
      <ul class="pf-changelog" style="margin-top:8px">`;
  if (changelog.length) {
    changelog.forEach(c => {
      html += `<li class="pf-changelog__item">
        <span class="pf-changelog__dot"></span>
        <div class="pf-changelog__body">
          <span class="pf-changelog__field">${escHtmlGlobal(c.field)}</span>
          <span class="pf-changelog__values">${escHtmlGlobal(c.old_value || '—')} → ${escHtmlGlobal(c.new_value || '—')}</span>
          <span class="pf-changelog__time">${c.changed_at ? c.changed_at.replace('T',' ').slice(0,16) : ''} by ${escHtmlGlobal(c.changed_by || '—')}</span>
        </div>
      </li>`;
    });
  } else {
    html += '<li style="font-size:12px;color:var(--color-fg-muted);padding:4px 0">No changes recorded</li>';
  }
  html += '</ul></details></div>';

  // Actions
  const transitions = (typeof PM_TRANSITIONS !== 'undefined') ? (PM_TRANSITIONS[f.status] || []) : [];
  if (transitions.length) {
    html += `<div style="padding-top:16px;border-top:1px solid var(--color-border-default);margin-top:8px">
      <div style="font-size:12px;font-weight:600;color:var(--color-fg-subtle);margin-bottom:8px">Change status</div>
      <div style="display:flex;gap:6px;flex-wrap:wrap">`;
    transitions.forEach(s => {
      html += `<form method="post" action="/product/${PRODUCT_ID}/features/${f.id}/status" style="display:inline">
        <button name="status" value="${s}" class="btn btn-sm btn-outline">${s}</button>
      </form>`;
    });
    html += '</div></div>';
  }

  body.innerHTML = html;
}

// ── Comment ──────────────────────────────────────────────
async function addComment(featureId) {
  const textarea = document.getElementById('fp-comment-text');
  const body = textarea.value.trim();
  if (!body) return;
  try {
    await fetch(`/api/features/${featureId}/comments`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ author: 'pm', body: body })
    });
    textarea.value = '';
    showToast('success', 'Comment added');
    openFeaturePanel(featureId); // refresh
  } catch(e) {
    showToast('error', 'Failed to add comment');
  }
}

// ── Labels ───────────────────────────────────────────────
async function showAddLabel(featureId) {
  const panel = document.getElementById('fp-add-label');
  const select = document.getElementById('fp-label-select');
  panel.style.display = '';
  try {
    const labels = await fetch(`/api/products/${PRODUCT_ID}/labels`).then(r => r.json());
    select.innerHTML = '<option value="">Select label...</option>' +
      labels.map(l => `<option value="${l.id}">${escHtmlGlobal(l.name)}</option>`).join('');
  } catch(e) {}
}

async function addSelectedLabel(featureId) {
  const select = document.getElementById('fp-label-select');
  const labelId = select.value;
  if (!labelId) return;
  try {
    await fetch(`/api/features/${featureId}/labels`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ label_id: parseInt(labelId) })
    });
    showToast('success', 'Label added');
    openFeaturePanel(featureId);
  } catch(e) {
    showToast('error', 'Failed to add label');
  }
}

async function removeLabel(featureId, labelId) {
  try {
    await fetch(`/api/features/${featureId}/labels/${labelId}`, { method: 'DELETE' });
    showToast('success', 'Label removed');
    openFeaturePanel(featureId);
  } catch(e) {
    showToast('error', 'Failed to remove label');
  }
}

// ── Links ────────────────────────────────────────────────
async function removeLink(featureId, linkId) {
  try {
    await fetch(`/api/features/${featureId}/links/${linkId}`, { method: 'DELETE' });
    showToast('success', 'Link removed');
    openFeaturePanel(featureId);
  } catch(e) {
    showToast('error', 'Failed to remove link');
  }
}

// ── Wire up feature card clicks ──────────────────────────
// Matches both the phase-card variant (cards on Sprints/Backlog) and the
// fbt-row variant (Feature Backlog table rows). Both expose data-feature-id.
document.addEventListener('click', function(e) {
  const card = e.target.closest('.phase-card[data-feature-id], .fbt-row[data-feature-id]');
  if (!card) return;
  // Don't open panel if clicking on a button, link, checkbox, or form element
  if (e.target.closest('button, a, input, select, form')) return;
  const featureId = card.dataset.featureId;
  if (featureId) openFeaturePanel(parseInt(featureId));
});
