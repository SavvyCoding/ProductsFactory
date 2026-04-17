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
            ? `<a href="${a.pr_url}" target="_blank" class="pr-link" onclick="event.stopPropagation()">PR #${a.pr_number} ↗</a>`
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

    body.innerHTML = metaHtml + actHtml + rvHtml;
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
