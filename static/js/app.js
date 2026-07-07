/* PostPilot PWA — vanilla JS, no build step */

const API = '/api';
let currentTab = 'queue';
let refreshTimer = null;
let watchlist = [];
let editingDraftId = null;

// ── API helpers ──────────────────────────────────────────

async function api(path, opts = {}) {
  const res = await fetch(`${API}${path}`, {
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', ...opts.headers },
    ...opts,
  });
  if (res.status === 401) {
    showLogin();
    throw new Error('Unauthorized');
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `Error ${res.status}`);
  return data;
}

// ── Auth ─────────────────────────────────────────────────

function showLogin() {
  document.getElementById('login-screen').classList.remove('hidden');
  document.getElementById('app').classList.add('hidden');
  stopRefresh();
}

function showApp() {
  document.getElementById('login-screen').classList.add('hidden');
  document.getElementById('app').classList.remove('hidden');
  startRefresh();
  loadCurrentTab();
}

document.getElementById('login-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const pw = document.getElementById('password-input').value;
  const errEl = document.getElementById('login-error');
  errEl.classList.add('hidden');
  try {
    await api('/login', { method: 'POST', body: JSON.stringify({ password: pw }) });
    showApp();
  } catch (err) {
    errEl.textContent = err.message;
    errEl.classList.remove('hidden');
  }
});

async function checkAuth() {
  try {
    await api('/me');
    showApp();
  } catch {
    showLogin();
  }
}

// ── Navigation ───────────────────────────────────────────

document.querySelectorAll('.nav-btn').forEach((btn) => {
  btn.addEventListener('click', () => {
    currentTab = btn.dataset.tab;
    document.querySelectorAll('.nav-btn').forEach((b) => b.classList.remove('active'));
    btn.classList.add('active');
    document.querySelectorAll('.tab-screen').forEach((s) => s.classList.add('hidden'));
    document.getElementById(`${currentTab}-screen`).classList.remove('hidden');
    const titles = { queue: 'Queue', history: 'History', settings: 'Settings' };
    const subtitles = {
      queue: 'Pending drafts',
      history: 'Posted & rejected',
      settings: 'Pipeline & limits',
    };
    document.getElementById('screen-title').textContent = titles[currentTab];
    document.getElementById('header-subtitle').textContent = subtitles[currentTab];
    loadCurrentTab();
  });
});

function loadCurrentTab() {
  if (currentTab === 'queue') loadQueue();
  else if (currentTab === 'history') loadHistory();
  else if (currentTab === 'settings') loadSettings();
}

function startRefresh() {
  stopRefresh();
  refreshTimer = setInterval(() => {
    if (currentTab === 'queue') loadQueue(true);
  }, 30000);
}

function stopRefresh() {
  if (refreshTimer) clearInterval(refreshTimer);
}

// ── Queue ────────────────────────────────────────────────

async function loadQueue(silent = false) {
  try {
    const data = await api('/queue');
    renderQueue(data.drafts);
    updateBadge(data.count);
  } catch (err) {
    if (!silent) showToast(err.message, 'error');
  }
}

function updateBadge(count) {
  const badge = document.getElementById('queue-badge');
  const navBadge = document.getElementById('nav-badge');
  if (count > 0) {
    badge.textContent = count;
    badge.classList.remove('hidden');
    navBadge.textContent = count;
    navBadge.classList.remove('hidden');
  } else {
    badge.classList.add('hidden');
    navBadge.classList.add('hidden');
  }
}

function renderQueue(drafts) {
  const list = document.getElementById('queue-list');
  const empty = document.getElementById('queue-empty');
  if (!drafts.length) {
    list.innerHTML = '';
    empty.classList.remove('hidden');
    return;
  }
  empty.classList.add('hidden');
  list.innerHTML = drafts.map((d) => renderDraftCard(d)).join('');
  attachCardListeners();
}

function renderDraftCard(d) {
  const confPct = Math.round(d.confidence * 100);
  const isEditing = editingDraftId === d.id;
  const formatClass = (d.format || 'CONTEXT').toLowerCase();
  const sourceLink = d.headline
    ? `<a href="${esc(d.headline.url)}" target="_blank" rel="noopener">${esc(d.headline.source)}</a>`
    : '';

  const tickerTags = d.tickers.length
    ? d.tickers.map((t) => `<span class="tag tag-ticker">$${esc(t)}</span>`).join('')
    : '';

  let actions;
  if (isEditing) {
    actions = `
      <textarea class="edit-area" id="edit-${d.id}" maxlength="500">${esc(d.text)}</textarea>
      <div class="char-counter" id="counter-${d.id}">${d.text.length}/280</div>
      <div class="card-actions editing">
        <button class="btn btn-success" data-action="approve-edit" data-id="${d.id}">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 6L9 17l-5-5"/></svg>
          Approve
        </button>
        <button class="btn btn-secondary" data-action="cancel-edit" data-id="${d.id}">Cancel</button>
      </div>`;
  } else {
    actions = `
      <div class="card-actions">
        <button class="btn btn-success" data-action="approve" data-id="${d.id}">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 6L9 17l-5-5"/></svg>
          Approve
        </button>
        <button class="btn btn-danger" data-action="reject" data-id="${d.id}">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 6L6 18M6 6l12 12"/></svg>
          Reject
        </button>
        <button class="btn btn-edit" data-action="edit" data-id="${d.id}" aria-label="Edit">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M11 4H4a2 2 0 00-2 2v14a2 2 0 002 2h14a2 2 0 002-2v-7"/><path d="M18.5 2.5a2.12 2.12 0 013 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>
        </button>
      </div>`;
  }

  return `
    <article class="draft-card impact-${esc(d.impact)}" data-id="${d.id}">
      <div class="draft-top">
        <div class="draft-meta">
          <span class="tag tag-format tag-format-${formatClass}">${esc(d.format)}</span>
          <span class="tag tag-impact-${esc(d.impact)}">${esc(d.impact)}</span>
          <span class="tag tag-category">${esc(d.category)}</span>
          ${tickerTags}
        </div>
        <span class="draft-age">${esc(d.age)}</span>
      </div>
      ${isEditing ? '' : `<div class="draft-text format-${formatClass}">${esc(d.text)}</div>`}
      ${sourceLink ? `<div class="draft-source">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 13v6a2 2 0 01-2 2H5a2 2 0 01-2-2V8a2 2 0 012-2h6"/><path d="M15 3h6v6M10 14L21 3"/></svg>
        ${sourceLink}
      </div>` : ''}
      <div class="confidence-row">
        <div class="confidence-bar"><div class="confidence-fill" style="width:${confPct}%"></div></div>
        <span class="confidence-label">${confPct}%</span>
      </div>
      ${actions}
    </article>`;
}

function attachCardListeners() {
  document.querySelectorAll('[data-action]').forEach((btn) => {
    btn.addEventListener('click', handleCardAction);
  });
  document.querySelectorAll('.edit-area').forEach((ta) => {
    ta.addEventListener('input', () => {
      const id = ta.id.replace('edit-', '');
      const counter = document.getElementById(`counter-${id}`);
      const len = ta.value.length;
      counter.textContent = `${len}/280`;
      counter.classList.toggle('over', len > 280 && !ta.value.includes('\n\n'));
    });
  });
}

async function handleCardAction(e) {
  const btn = e.currentTarget;
  const action = btn.dataset.action;
  const id = parseInt(btn.dataset.id);

  if (action === 'approve') {
    btn.disabled = true;
    try {
      const res = await api(`/drafts/${id}/approve`, { method: 'POST', body: '{}' });
      showToast('Posted!' + (res.tweet_url ? '' : ' (dry run)'), 'success');
      loadQueue();
    } catch (err) {
      showToast(err.message, 'error');
      btn.disabled = false;
    }
  } else if (action === 'reject') {
    btn.disabled = true;
    try {
      await api(`/drafts/${id}/reject`, { method: 'POST' });
      showToast('Rejected', 'success');
      loadQueue();
    } catch (err) {
      showToast(err.message, 'error');
      btn.disabled = false;
    }
  } else if (action === 'edit') {
    editingDraftId = id;
    loadQueue();
  } else if (action === 'cancel-edit') {
    editingDraftId = null;
    loadQueue();
  } else if (action === 'approve-edit') {
    const ta = document.getElementById(`edit-${id}`);
    const text = ta.value.trim();
    if (!text) { showToast('Text cannot be empty', 'error'); return; }
    btn.disabled = true;
    try {
      const res = await api(`/drafts/${id}/approve`, {
        method: 'POST',
        body: JSON.stringify({ text }),
      });
      editingDraftId = null;
      showToast('Posted!' + (res.tweet_url ? '' : ' (dry run)'), 'success');
      loadQueue();
    } catch (err) {
      showToast(err.message, 'error');
      btn.disabled = false;
    }
  }
}

// ── Pull to refresh ──────────────────────────────────────

let touchStartY = 0;
const queueScreen = document.getElementById('queue-screen');
queueScreen.addEventListener('touchstart', (e) => {
  touchStartY = e.touches[0].clientY;
}, { passive: true });
queueScreen.addEventListener('touchmove', (e) => {
  const diff = e.touches[0].clientY - touchStartY;
  if (diff > 60 && window.scrollY === 0) {
    document.getElementById('pull-indicator').classList.remove('hidden');
  }
}, { passive: true });
queueScreen.addEventListener('touchend', async () => {
  const indicator = document.getElementById('pull-indicator');
  if (!indicator.classList.contains('hidden')) {
    indicator.classList.add('hidden');
    await loadQueue();
    showToast('Refreshed', 'success');
  }
});

// ── History ──────────────────────────────────────────────

async function loadHistory() {
  try {
    const data = await api('/history');
    renderHistory(data);
  } catch (err) {
    showToast(err.message, 'error');
  }
}

function renderHistory(data) {
  const posted = data.stats.posted_today;
  const cap = data.stats.daily_cap;
  const pct = Math.min(100, Math.round((posted / cap) * 100));

  document.getElementById('stats-value').textContent = `${posted} / ${cap}`;
  document.getElementById('stats-ring').style.setProperty('--progress', `${pct}%`);

  const postedList = document.getElementById('posted-list');
  const postedEmpty = document.getElementById('posted-empty');
  if (data.posted.length) {
    postedEmpty.classList.add('hidden');
    postedList.innerHTML = data.posted.map((p) => `
      <div class="history-card">
        <div class="draft-text">${esc(p.text)}</div>
        <div class="history-footer">
          <span class="history-time">${formatDate(p.posted_at)}</span>
          ${p.tweet_url
            ? `<a class="history-link" href="${esc(p.tweet_url)}" target="_blank" rel="noopener">
                View on X
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 13v6a2 2 0 01-2 2H5a2 2 0 01-2-2V8a2 2 0 012-2h6"/><path d="M15 3h6v6M10 14L21 3"/></svg>
              </a>`
            : '<span class="muted">Dry run</span>'}
        </div>
      </div>`).join('');
  } else {
    postedList.innerHTML = '';
    postedEmpty.classList.remove('hidden');
  }

  document.getElementById('rejected-count').textContent = data.rejected.length;
  const rejectedList = document.getElementById('rejected-list');
  rejectedList.innerHTML = data.rejected.map((r) => `
    <div class="rejected-card">
      <div class="draft-text">${esc(r.text)}</div>
      <div class="history-time">${formatDate(r.created_at)}</div>
    </div>`).join('');
}

document.getElementById('rejected-toggle').addEventListener('click', () => {
  const list = document.getElementById('rejected-list');
  const toggle = document.getElementById('rejected-toggle');
  list.classList.toggle('collapsed');
  toggle.classList.toggle('open');
});

// ── Settings ─────────────────────────────────────────────

async function loadSettings() {
  try {
    const data = await api('/settings');
    document.getElementById('pipeline-toggle').checked = data.pipeline_enabled;
    document.getElementById('daily-cap').value = data.daily_post_cap;
    document.getElementById('cooldown').value = data.cooldown_minutes;
    watchlist = data.watchlist || [];
    renderWatchlist();

    const pauseEl = document.getElementById('pause-status');
    if (data.paused_until) {
      pauseEl.textContent = `Paused until ${formatDate(data.paused_until)}`;
      pauseEl.classList.remove('hidden');
    } else {
      pauseEl.textContent = '';
      pauseEl.classList.add('hidden');
    }

    const cfg = data.config || {};
    const items = [
      { label: 'Dry run', on: cfg.dry_run },
      { label: 'Anthropic', on: cfg.anthropic_configured },
      { label: 'X API', on: cfg.x_configured },
      { label: 'Finnhub', on: cfg.finnhub_configured },
    ];
    document.getElementById('config-info').innerHTML = items.map((i) => `
      <div class="status-pill">
        <span class="status-dot ${i.on ? 'on' : 'off'}"></span>
        ${esc(i.label)}
      </div>`).join('');
  } catch (err) {
    showToast(err.message, 'error');
  }
}

function renderWatchlist() {
  const container = document.getElementById('watchlist-chips');
  container.innerHTML = watchlist.map((t) => `
    <span class="chip">$${esc(t)}
      <button class="chip-remove" data-ticker="${esc(t)}">&times;</button>
    </span>`).join('');
  container.querySelectorAll('.chip-remove').forEach((btn) => {
    btn.addEventListener('click', () => {
      watchlist = watchlist.filter((t) => t !== btn.dataset.ticker);
      renderWatchlist();
    });
  });
}

document.getElementById('add-ticker').addEventListener('click', () => {
  const input = document.getElementById('watchlist-input');
  const val = input.value.trim().toUpperCase().replace(/^\$/, '');
  if (val && !watchlist.includes(val)) {
    watchlist.push(val);
    renderWatchlist();
  }
  input.value = '';
});

document.getElementById('save-settings').addEventListener('click', async () => {
  try {
    await api('/settings', {
      method: 'PATCH',
      body: JSON.stringify({
        pipeline_enabled: document.getElementById('pipeline-toggle').checked,
        daily_post_cap: parseInt(document.getElementById('daily-cap').value),
        cooldown_minutes: parseInt(document.getElementById('cooldown').value),
        watchlist,
      }),
    });
    showToast('Settings saved', 'success');
  } catch (err) {
    showToast(err.message, 'error');
  }
});

document.querySelectorAll('.pause-btn').forEach((btn) => {
  btn.addEventListener('click', async () => {
    let pausedUntil;
    if (btn.dataset.tomorrow) {
      const d = new Date();
      d.setDate(d.getDate() + 1);
      d.setHours(8, 0, 0, 0);
      pausedUntil = d.toISOString();
    } else {
      const hours = parseInt(btn.dataset.hours);
      pausedUntil = new Date(Date.now() + hours * 3600000).toISOString();
    }
    try {
      await api('/settings', {
        method: 'PATCH',
        body: JSON.stringify({ paused_until: pausedUntil }),
      });
      showToast('Pipeline paused', 'success');
      loadSettings();
    } catch (err) {
      showToast(err.message, 'error');
    }
  });
});

// ── Utilities ────────────────────────────────────────────

function esc(str) {
  const el = document.createElement('span');
  el.textContent = str || '';
  return el.innerHTML;
}

function formatDate(iso) {
  const d = new Date(iso);
  const now = new Date();
  const diff = (now - d) / 1000;
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

let toastTimer;
function showToast(msg, type = '') {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.className = 'toast' + (type ? ` ${type}` : '');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add('hidden'), 3000);
  el.classList.remove('hidden');
}

// ── Service Worker ───────────────────────────────────────

if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/sw.js').catch(() => {});
}

// ── Init ─────────────────────────────────────────────────

checkAuth();
