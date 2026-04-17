// ── Global Feature Search ─────────────────────────────────
(function() {
  const input = document.getElementById('global-search');
  if (!input) return;

  let dropdown = null;
  let debounceTimer = null;

  function createDropdown() {
    if (dropdown) return dropdown;
    dropdown = document.createElement('div');
    dropdown.id = 'search-dropdown';
    dropdown.style.cssText = `
      position:absolute;top:100%;left:0;right:0;
      background:var(--color-canvas-overlay);
      border:1px solid var(--color-border-default);
      border-radius:0 0 var(--radius-md) var(--radius-md);
      box-shadow:var(--shadow-lg);max-height:400px;overflow-y:auto;
      z-index:200;display:none;
    `;
    input.closest('.pf-nav-search').appendChild(dropdown);
    return dropdown;
  }

  function showResults(results, query) {
    const dd = createDropdown();
    if (!results.length) {
      dd.innerHTML = `<div style="padding:12px 16px;font-size:13px;color:var(--color-fg-muted)">No results for "${escHtmlGlobal(query)}"</div>`;
      dd.style.display = '';
      return;
    }

    // Group by product
    const groups = {};
    results.forEach(f => {
      const key = f.product_name || `Product #${f.product_id}`;
      if (!groups[key]) groups[key] = { product_id: f.product_id, features: [] };
      groups[key].features.push(f);
    });

    let html = '';
    Object.entries(groups).forEach(([name, group]) => {
      html += `<div style="padding:4px 16px;font-size:11px;font-weight:600;color:var(--color-fg-subtle);text-transform:uppercase;letter-spacing:0.5px;background:var(--color-canvas-subtle)">${escHtmlGlobal(name)}</div>`;
      group.features.slice(0, 5).forEach(f => {
        html += `<a href="/product/${f.product_id}?tab=board"
          style="display:flex;align-items:center;gap:8px;padding:8px 16px;font-size:13px;color:var(--color-fg-default);text-decoration:none;cursor:pointer;border-bottom:1px solid var(--color-border-muted)"
          onmouseover="this.style.background='var(--color-canvas-subtle)'"
          onmouseout="this.style.background=''">
          <span class="badge badge-status-${f.status.toLowerCase()}" style="font-size:10px">${escHtmlGlobal(f.status)}</span>
          <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${escHtmlGlobal(f.name)}</span>
          ${f.feature_type !== 'feature' ? `<span class="type-badge type-${f.feature_type}" style="font-size:9px">${f.feature_type}</span>` : ''}
        </a>`;
      });
    });

    dd.innerHTML = html;
    dd.style.display = '';
  }

  function hideDropdown() {
    if (dropdown) dropdown.style.display = 'none';
  }

  async function doSearch(query) {
    if (query.length < 2) { hideDropdown(); return; }
    try {
      const r = await fetch(`/api/features/search?q=${encodeURIComponent(query)}`);
      if (!r.ok) return;
      const results = await r.json();
      showResults(results, query);
    } catch(e) { /* silent */ }
  }

  input.addEventListener('input', () => {
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(() => doSearch(input.value.trim()), 300);
  });

  input.addEventListener('focus', () => {
    if (input.value.trim().length >= 2 && dropdown) dropdown.style.display = '';
  });

  // Close on click outside
  document.addEventListener('click', (e) => {
    if (!e.target.closest('.pf-nav-search')) hideDropdown();
  });

  // Close on Escape
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { hideDropdown(); input.blur(); }
  });

  // "/" keyboard shortcut to focus search
  document.addEventListener('keydown', (e) => {
    if (e.key === '/' && !e.target.matches('input, textarea, select')) {
      e.preventDefault();
      input.focus();
    }
  });
})();
