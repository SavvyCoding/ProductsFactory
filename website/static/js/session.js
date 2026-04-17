// ── VIDEOS ────────────────────────────────────────────────
let _videosLoaded = false;

async function loadVideos() {
  if (_videosLoaded) return;
  _videosLoaded = true;
  const loading = document.getElementById('videos-loading');
  const grid    = document.getElementById('videos-grid');
  const empty   = document.getElementById('videos-empty');
  try {
    const r = await fetch(`/api/products/${PRODUCT_ID}/videos`);
    const videos = await r.json();
    loading.style.display = 'none';
    if (!videos.length) { empty.style.display = ''; return; }
    grid.style.display = '';
    grid.innerHTML = videos.map(v => `
      <div class="video-card">
        <video controls preload="metadata" style="width:100%;border-radius:6px;background:#000">
          <source src="${v.url}" type="video/mp4">
        </video>
        <div class="video-meta">
          <span class="video-name">${v.filename}</span>
          <span class="video-size">${v.size_mb} MB</span>
          <a href="${v.url}" download class="btn btn-outline" style="padding:3px 10px;font-size:12px">⬇ Download</a>
        </div>
      </div>`).join('');
  } catch(e) {
    loading.textContent = 'Failed to load videos.';
  }
}

(function() {
  const params = new URLSearchParams(window.location.search);
  if (params.get('tab') === 'videos') loadVideos();
})();

// ── SESSION LOG STREAMING ─────────────────────────────────
let evtSource   = null;
let sessionStart = null;
let elapsedTimer = null;
let thinkingTimer = null;
let _atBottom = true;

function scrollToBottom() {
  const t = document.getElementById('session-terminal');
  t.scrollTop = t.scrollHeight;
  document.getElementById('new-msg-pill').classList.remove('visible');
  document.getElementById('scroll-btn').classList.remove('visible');
  _atBottom = true;
}

function toggleSession() {
  if (evtSource) {
    evtSource.close(); evtSource = null;
    clearInterval(elapsedTimer); elapsedTimer = null;
    clearTimeout(thinkingTimer);
    document.getElementById('session-dot').classList.remove('live');
    document.getElementById('session-toggle').textContent = 'Connect';
    document.getElementById('session-elapsed').style.display = 'none';
    document.getElementById('session-thinking').classList.remove('visible');
    return;
  }
  const terminal = document.getElementById('session-terminal');
  terminal.innerHTML = '';
  _atBottom = true;
  sessionStart = Date.now();
  evtSource = new EventSource(`/api/products/${PRODUCT_ID}/session/stream`);
  document.getElementById('session-dot').classList.add('live');
  document.getElementById('session-toggle').textContent = 'Disconnect';

  const elapsedEl = document.getElementById('session-elapsed');
  elapsedEl.style.display = '';
  elapsedTimer = setInterval(() => {
    const s = Math.floor((Date.now() - sessionStart) / 1000);
    elapsedEl.textContent = Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0');
  }, 1000);

  function resetThinking() {
    clearTimeout(thinkingTimer);
    document.getElementById('session-thinking').classList.remove('visible');
    thinkingTimer = setTimeout(() => {
      if (evtSource) document.getElementById('session-thinking').classList.add('visible');
    }, 3000);
  }
  resetThinking();

  evtSource.onmessage = (e) => {
    const line = JSON.parse(e.data);
    appendLine(terminal, line);
    resetThinking();
  };
  evtSource.onerror = () => {
    appendLine(terminal, '— connection lost —', 'err');
    document.getElementById('session-dot').classList.remove('live');
    document.getElementById('session-thinking').classList.remove('visible');
  };

  terminal.addEventListener('scroll', () => {
    const atBottom = terminal.scrollHeight - terminal.scrollTop - terminal.clientHeight < 40;
    _atBottom = atBottom;
    if (atBottom) {
      document.getElementById('new-msg-pill').classList.remove('visible');
      document.getElementById('scroll-btn').classList.remove('visible');
    } else {
      document.getElementById('scroll-btn').classList.add('visible');
    }
  });
}

function appendLine(terminal, text, cls) {
  const empty = terminal.querySelector('.log-empty');
  if (empty) empty.remove();
  const el = document.createElement('span');
  let lineCls = 'log-line';
  if (cls) lineCls += ' ' + cls;
  else if (text.startsWith('[PM]')) lineCls += ' pm-msg';
  else if (/error|failed|exception/i.test(text)) lineCls += ' err';
  else if (/^\[Tool:/i.test(text)) lineCls += ' tool-call';
  el.className = lineCls;
  el.textContent = text;
  terminal.appendChild(el);
  terminal.appendChild(document.createElement('br'));
  if (_atBottom) {
    terminal.scrollTop = terminal.scrollHeight;
  } else {
    document.getElementById('new-msg-pill').classList.add('visible');
  }
}

async function sendMessage() {
  const input = document.getElementById('session-msg');
  const msg = input.value.trim();
  if (!msg) return;
  input.value = '';
  try {
    await fetch(`/api/products/${PRODUCT_ID}/session/message`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ message: msg }),
    });
    showToast('info', 'Message sent to Claude.');
  } catch(e) {
    showToast('error', 'Failed to send message.');
  }
}

// ── TOOL CALL STYLING ─────────────────────────────────────
(function() {
  const s = document.createElement('style');
  s.textContent = '.session-terminal .log-line.tool-call { color: #818cf8; }';
  document.head.appendChild(s);
})();
