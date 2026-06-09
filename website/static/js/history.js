// ── HISTORY: row expand ──────────────────────────────────
const _histLoaded = new Set();
const STATUS_ICONS = {
  Pushed:'✅', Reviewing:'🔍', Reviewed:'🔍', Designed:'✏️', Designing:'✏️',
  Implementing:'⚙️', Approved:'👍', Pending:'⏳', Blocked:'🚫',
  Deferred:'⏸️', Rejected:'❌', Reverted:'↩️',
};

async function toggleHistoryRow(row) {
  const sid = row.dataset.sessionId;
  const detailRow = document.getElementById('hist-detail-' + sid);
  const chevron = row.querySelector('.hist-chevron');
  const open = detailRow.style.display !== 'none';
  if (open) {
    detailRow.style.display = 'none';
    row.classList.remove('hist-open');
    chevron.textContent = '▶';
    return;
  }
  detailRow.style.display = 'table-row';
  row.classList.add('hist-open');
  chevron.textContent = '▼';
  if (_histLoaded.has(sid)) return;
  _histLoaded.add(sid);
  try {
    const r = await fetch('/api/sessions/' + sid);
    const d = await r.json();
    const body = document.getElementById('hist-detail-body-' + sid);

    const fmtDur = s => s == null ? null : `${Math.floor(s/60)}m ${s%60}s`;
    const fmtTime = s => s ? s.replace('T',' ').slice(0,19) : null;

    const meta = [
      ['Persona',   d.persona || '—'],
      ['Started',   fmtTime(d.started_at) || '—'],
      ['Ended',     fmtTime(d.ended_at) || 'Still running'],
      ['Duration',  fmtDur(d.duration_seconds) || '—'],
      ['Exit code', d.exit_code != null ? (d.exit_code === 0 ? '✓ 0 (OK)' : `✗ ${d.exit_code}`) : '—'],
      d.tokens_input || d.tokens_output
        ? ['Tokens', `${(d.tokens_input||0).toLocaleString()} in / ${(d.tokens_output||0).toLocaleString()} out`]
        : null,
      d.cost_usd != null ? ['Cost', '$' + d.cost_usd.toFixed(4)] : null,
      d.notes ? ['Notes', d.notes] : null,
    ].filter(Boolean);

    const metaHtml = `<div class="hist-meta-strip">` +
      meta.map(([k,v]) => `<div class="hist-kv"><span>${k}</span><span>${escHtmlGlobal(String(v))}</span></div>`).join('') +
      `</div>`;

    let actHtml = '';
    if (d.activities && d.activities.length) {
      const reviewMap = {};
      (d.reviews || []).forEach(rv => { reviewMap[rv.feature_name] = rv; });

      actHtml = `<div class="hist-section-title">Features touched (${d.activities.length})</div>
        <div class="hist-timeline">` +
        d.activities.map(a => {
          const icon = STATUS_ICONS[a.status] || '•';
          const rv = reviewMap[a.name];
          const rvBadge = rv
            ? `<span class="hist-review-outcome ${rv.outcome==='approved'?'rv-ok':'rv-changes'}">${rv.outcome==='approved'?'✓ Approved':'↩ Changes'}</span>`
            : '';
          const prLink = a.pr_url
            ? `<a href="${a.pr_url}" target="_blank" class="pr-link" onclick="event.stopPropagation()">PR #${a.pr_number || a.pr_url.split('/').pop()} ↗</a>`
            : '';
          const typeLabel = a.feature_type !== 'feature'
            ? `<span class="type-badge type-${a.feature_type}">${a.feature_type}</span>` : '';
          const rvNotes = rv && rv.notes
            ? `<div class="hist-review-notes">${escHtmlGlobal(rv.notes.slice(0,200))}${rv.notes.length>200?'…':''}</div>` : '';
          return `<div class="hist-act-row">
            <span class="hist-act-icon">${icon}</span>
            <div class="hist-act-body">
              <span class="hist-act-name">${escHtmlGlobal(a.name)}</span>
              <span class="badge badge-status-${a.status.toLowerCase()}">${a.status}</span>
              ${typeLabel}${rvBadge}${prLink}
              ${rvNotes}
            </div>
            <span class="hist-act-time">${fmtTime(a.updated_at).slice(11)}</span>
          </div>`;
        }).join('') + `</div>`;
    } else if (!d.reviews || !d.reviews.length) {
      actHtml = `<div class="hist-empty-act">No feature activity recorded for this session.</div>`;
    }

    const actNames = new Set((d.activities||[]).map(a=>a.name));
    const extraReviews = (d.reviews||[]).filter(rv => !actNames.has(rv.feature_name));
    let rvHtml = '';
    if (extraReviews.length) {
      rvHtml = `<div class="hist-section-title">Code Reviews</div><div class="hist-reviews">` +
        extraReviews.map(rv => `<div class="hist-review-item">
          <span class="hist-review-feature">${escHtmlGlobal(rv.feature_name)}</span>
          <span class="hist-review-outcome ${rv.outcome==='approved'?'rv-ok':'rv-changes'}">${rv.outcome==='approved'?'✓ Approved':'↩ Changes requested'}</span>
          ${rv.notes?`<div class="hist-review-notes">${escHtmlGlobal(rv.notes.slice(0,200))}${rv.notes.length>200?'…':''}</div>`:''}
        </div>`).join('') + `</div>`;
    }

    // Lifecycle events timeline (launched → running → ended/killed)
    let evHtml = '';
    try {
      const er = await fetch('/api/sessions/' + sid + '/events');
      if (er.ok) {
        const events = await er.json();
        if (events.length) {
          const EVENT_ICON = {launched:'🚀', running:'▶️', heartbeat:'💓',
                              ended:'✅', killed:'⛔', orphaned:'👻', reconciled:'🔄'};
          evHtml = `<div class="hist-section-title">Lifecycle events (${events.length})</div>
            <div class="hist-events">` +
            events.map(e => {
              const icon = EVENT_ICON[e.event] || '•';
              const t = fmtTime(e.created_at) || '';
              const detail = e.detail ? `<span class="hist-event-detail">${escHtmlGlobal(e.detail)}</span>` : '';
              return `<div class="hist-event-row">
                <span class="hist-event-icon">${icon}</span>
                <span class="hist-event-name">${escHtmlGlobal(e.event)}</span>
                ${detail}
                <span class="hist-event-time">${t.slice(11)}</span>
              </div>`;
            }).join('') + `</div>`;
        }
      }
    } catch(e) { /* non-fatal */ }

    // Changelog entries inside the session window — per-field state transitions
    // attributable to this run (status flips, sprint moves, priority bumps).
    let clHtml = '';
    if (d.changelog && d.changelog.length) {
      clHtml = `<div class="hist-section-title">Field changes (${d.changelog.length})</div>
        <div class="hist-events">` +
        d.changelog.map(c => {
          const t = fmtTime(c.changed_at) || '';
          const oldV = c.old_value == null ? '—' : c.old_value;
          const newV = c.new_value == null ? '—' : c.new_value;
          return `<div class="hist-event-row">
            <span class="hist-event-icon">🔧</span>
            <span class="hist-event-name">#${c.feature_id} ${escHtmlGlobal(c.field)}</span>
            <span class="hist-event-detail">${escHtmlGlobal(oldV)} → ${escHtmlGlobal(newV)} (by ${escHtmlGlobal(c.changed_by||'agent')})</span>
            <span class="hist-event-time">${t.slice(11)}</span>
          </div>`;
        }).join('') + `</div>`;
    }

    // Comments left during the session window — usually agent-authored notes.
    let cmHtml = '';
    if (d.comments && d.comments.length) {
      cmHtml = `<div class="hist-section-title">Comments (${d.comments.length})</div>
        <div class="hist-events">` +
        d.comments.map(c => {
          const t = fmtTime(c.created_at) || '';
          const body = c.body.length > 200 ? c.body.slice(0,200) + '…' : c.body;
          return `<div class="hist-event-row">
            <span class="hist-event-icon">💬</span>
            <span class="hist-event-name">#${c.feature_id} ${escHtmlGlobal(c.author||'?')}</span>
            <span class="hist-event-detail">${escHtmlGlobal(body)}</span>
            <span class="hist-event-time">${t.slice(11)}</span>
          </div>`;
        }).join('') + `</div>`;
    }

    // Agent transcript — best-effort snapshot of stdout filtered by session_uid.
    // For finished sessions this comes from sessions.log (snapshotted at close);
    // for running sessions it tails the in-memory PM API buffer.
    let logHtml = '';
    if (d.log) {
      const lineCount = d.log.split('\n').length;
      logHtml = `<div class="hist-section-title">Agent log (${lineCount} line${lineCount === 1 ? '' : 's'})</div>
        <details class="hist-log-details">
          <summary>Show transcript</summary>
          <pre class="hist-log-pre">${escHtmlGlobal(d.log)}</pre>
        </details>`;
    }

    body.innerHTML = metaHtml + evHtml + actHtml + clHtml + cmHtml + rvHtml + logHtml;
  } catch(e) {
    document.getElementById('hist-detail-body-' + sid).innerHTML =
      `<span style="color:var(--red)">Failed to load details: ${escHtmlGlobal(String(e))}</span>`;
  }
}

async function clearHistory(productId) {
  if (!confirm('Delete all session history for this product?')) return;
  try {
    const r = await fetch(`/api/products/${productId}/session/log`, {method:'DELETE'});
    const d = await r.json();
    if (r.ok) {
      document.querySelector('#tab-history table')?.remove();
      document.querySelector('#tab-history .btn-danger')?.parentElement?.remove();
      document.getElementById('tab-history').innerHTML +=
        '<div class="empty-state"><div class="empty-icon">📂</div><h2>No sessions yet</h2><p>Sessions appear here once the poller runs Claude on this product.</p></div>';
      showToast('success', `Cleared ${d.deleted} session(s)`);
    } else {
      showToast('error', 'Failed to clear history');
    }
  } catch(e) { showToast('error', 'Failed to clear history'); }
}
