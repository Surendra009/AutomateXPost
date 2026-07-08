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

document.querySelectorAll('.tab').forEach((btn) => {
  btn.addEventListener('click', () => {
    currentTab = btn.dataset.tab;
    document.querySelectorAll('.tab').forEach((b) => b.classList.remove('active'));
    btn.classList.add('active');
    document.querySelectorAll('.page-section').forEach((s) => s.classList.add('hidden'));
    document.getElementById(`${currentTab}-screen`).classList.remove('hidden');
    const titles = { queue: 'Queue', history: 'History', settings: 'Settings' };
    const subtitles = {
      queue: 'Review pending drafts',
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
  const sub = document.getElementById('header-subtitle');
  if (count > 0) {
    badge.textContent = count;
    badge.classList.remove('hidden');
    navBadge.textContent = count;
    navBadge.classList.remove('hidden');
    if (currentTab === 'queue') sub.textContent = `${count} draft${count === 1 ? '' : 's'} waiting`;
  } else {
    badge.classList.add('hidden');
    navBadge.classList.add('hidden');
    if (currentTab === 'queue') sub.textContent = 'Review pending drafts';
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
  const fmt = (d.format || 'CONTEXT').toLowerCase();
  const textClass = fmt === 'breaking' ? 'is-breaking' : fmt === 'summary' ? 'is-summary' : '';
  const source = d.headline
    ? `<a href="${esc(d.headline.url)}" target="_blank" rel="noopener">${esc(d.headline.source)}</a>`
    : 'Unknown source';

  const tags = [
    `<span class="tag tag-fmt-${fmt}">${esc(d.format)}</span>`,
    `<span class="tag tag-${esc(d.impact)}">${esc(d.impact)}</span>`,
    `<span class="tag tag-cat-${esc(d.category)}">${esc(d.category)}</span>`,
    ...d.tickers.map((t) => `<span class="tag tag-ticker">$${esc(t)}</span>`),
  ].join('');

  let body;
  if (isEditing) {
    body = `
      <div class="post-foot">
        <textarea class="edit-box" id="edit-${d.id}" maxlength="500">${esc(d.text)}</textarea>
        <div class="char-count" id="counter-${d.id}">${d.text.length}/280</div>
        <div class="post-actions">
          <button class="btn btn-approve btn-block" data-action="approve-edit" data-id="${d.id}">Approve edit</button>
          <button class="btn btn-secondary btn-block" data-action="cancel-edit" data-id="${d.id}">Cancel</button>
        </div>
      </div>`;
  } else {
    body = `
      <div class="post-body">
        <div class="post-text ${textClass}">${esc(d.text)}</div>
      </div>
      <div class="post-foot">
        <div class="post-tags">${tags}</div>
        <div class="post-meta">
          <div class="conf-track"><div class="conf-fill" style="width:${confPct}%"></div></div>
          <span>${confPct}% confidence</span>
        </div>
        <div class="post-actions">
          <button class="btn btn-approve btn-block" data-action="approve" data-id="${d.id}">Approve &amp; post</button>
          <div class="post-actions-row">
            <button class="btn btn-reject" data-action="reject" data-id="${d.id}">Reject</button>
            <button class="btn btn-edit" data-action="edit" data-id="${d.id}">Edit</button>
          </div>
        </div>
      </div>`;
  }

  const storyMins = d.story_age_minutes ?? 0;
  const ageClass = storyMins >= 240 ? 'is-stale' : storyMins >= 180 ? 'is-aging' : '';
  const storyLabel = d.story_age || d.age;

  return `
    <article class="post-card" data-id="${d.id}" data-impact="${esc(d.impact)}">
      <div class="post-head">
        <span class="post-source">${source}${d.is_seed ? ' <span class="seed-badge">Sample</span>' : ''}</span>
        <div class="post-head-actions">
          <button type="button" class="btn-icon" data-action="copy" data-id="${d.id}" title="Copy text" aria-label="Copy draft text">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/></svg>
          </button>
          <span class="post-age ${ageClass}" title="Draft created ${esc(d.age)} ago">
            <span class="post-age-label">Story</span> ${esc(storyLabel)}
          </span>
        </div>
      </div>
      ${body}
    </article>`;
}

function attachCardListeners() {
  document.querySelectorAll('[data-action]').forEach((btn) => {
    btn.addEventListener('click', handleCardAction);
  });
  document.querySelectorAll('.edit-box').forEach((ta) => {
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
  } else if (action === 'copy') {
    const card = btn.closest('.post-card');
    const text = card.querySelector('.post-text')?.textContent
      || card.querySelector('.edit-box')?.value
      || '';
    if (!text) {
      showToast('Nothing to copy', 'error');
      return;
    }
    try {
      await navigator.clipboard.writeText(text);
      showToast('Copied to clipboard', 'success');
    } catch {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.position = 'fixed';
      ta.style.left = '-9999px';
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      document.body.removeChild(ta);
      showToast('Copied to clipboard', 'success');
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
  document.getElementById('stats-bar').style.width = `${pct}%`;

  const postedList = document.getElementById('posted-list');
  const postedEmpty = document.getElementById('posted-empty');
  if (data.posted.length) {
    postedEmpty.classList.add('hidden');
    postedList.innerHTML = data.posted.map((p) => `
      <div class="list-item">
        <p>${esc(p.text)}</p>
        <div class="list-item-foot">
          <span>${formatDate(p.posted_at)}</span>
          ${p.tweet_url
            ? `<a href="${esc(p.tweet_url)}" target="_blank" rel="noopener">View on X</a>`
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
    <div class="list-item muted-item">
      <p>${esc(r.text)}</p>
      <div class="list-item-foot"><span>${formatDate(r.created_at)}</span></div>
    </div>`).join('');
}

document.getElementById('rejected-toggle').addEventListener('click', () => {
  const list = document.getElementById('rejected-list');
  const toggle = document.getElementById('rejected-toggle');
  list.classList.toggle('collapsed');
  toggle.classList.toggle('open');
});

// ── Settings ─────────────────────────────────────────────

function renderFinnhubStatus(fh) {
  const el = document.getElementById('finnhub-status');
  if (!fh || !fh.configured && !fh.error && !fh.news) {
    el.innerHTML = '<div class="status-row"><span>Status</span><span class="status-no">Not tested</span></div>';
    return;
  }
  const rows = [];
  if (fh.configured) {
    rows.push(`<div class="status-row"><span>API key</span><span class="status-ok">${esc(fh.env_var || 'FINNHUB_KEY')} ${esc(fh.key_hint || '')}</span></div>`);
  } else {
    rows.push(`<div class="status-row"><span>API key</span><span class="status-no">Missing</span></div>`);
  }
  const fmt = (label, block) => {
    if (!block) return '';
    if (block.ok) return `<div class="status-row"><span>${label}</span><span class="status-ok">${block.count} items</span></div>`;
    return `<div class="status-row"><span>${label}</span><span class="status-no">Failed</span></div>`;
  };
  rows.push(fmt('News', fh.news));
  rows.push(fmt('Earnings', fh.earnings));
  rows.push(fmt('Macro calendar', fh.macro));
  rows.push(fmt('Company news', fh.company_news));
  if (fh.error) {
    rows.push(`<div class="pipeline-error">${esc(fh.error)}</div>`);
  }
  el.innerHTML = rows.join('');
}

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
    const pipe = data.pipeline || {};
    const sources = pipe.news_sources || [];
    document.getElementById('news-sources').innerHTML = sources.map((s) => `
      <div class="status-row">
        <span>${esc(s.name)}</span>
        <span class="${s.enabled ? 'status-ok' : 'status-no'}">${s.enabled ? 'On' : 'Off'}</span>
      </div>
      ${s.hint ? `<p class="source-hint">${esc(s.hint)}</p>` : ''}`).join('');

    const bySource = pipe.last_ingest_by_source || {};
    const sourceDetail = Object.entries(bySource)
      .filter(([, n]) => n > 0)
      .map(([name, n]) => `${name}: ${n}`)
      .join(' · ');

    const items = [
      { label: 'Dry run', on: cfg.dry_run },
      { label: 'Anthropic', on: cfg.anthropic_configured },
      { label: 'X API', on: cfg.x_configured },
      { label: 'Finnhub', on: cfg.finnhub_configured },
    ];
    document.getElementById('config-info').innerHTML = items.map((i) => `
      <div class="status-row">
        <span>${esc(i.label)}</span>
        <span class="${i.on ? 'status-ok' : 'status-no'}">${i.on ? 'On' : 'Off'}</span>
      </div>`).join('');

    const lastRun = pipe.last_run_at ? formatDate(pipe.last_run_at) : 'Never';
    const sched = pipe.schedule || {};
    let schedLabel;
    if (sched.quiet_hours) {
      schedLabel = `Quiet (${sched.quiet_window || '10pm–5am'})`;
    } else if (sched.is_weekend) {
      schedLabel = `Weekend (every ${sched.weekend_interval_hours || 3}h)`;
    } else if (sched.next_mode === 'catchup') {
      schedLabel = 'Catch-up at 5am';
    } else {
      schedLabel = 'Active (6am–10pm)';
    }
    const err = pipe.last_error ? `<div class="pipeline-error">${esc(pipe.last_error)}</div>` : '';
    document.getElementById('pipeline-status').innerHTML = `
      <div class="status-row">
        <span>Schedule</span>
        <span>${esc(schedLabel)}${sched.timezone ? ` · ${esc(sched.timezone)}` : ''}</span>
      </div>
      <div class="status-row">
        <span>Last fetch</span>
        <span>${esc(lastRun)}</span>
      </div>
      <div class="status-row">
        <span>New headlines</span>
        <span>${pipe.last_ingest_count ?? 0}</span>
      </div>
      ${sourceDetail ? `<div class="pipeline-sources">${esc(sourceDetail)}</div>` : ''}
      <div class="status-row">
        <span>Drafts created</span>
        <span>${pipe.last_drafts_created ?? 0}</span>
      </div>
      ${pipe.last_expired ? `<div class="status-row"><span>Expired stale</span><span>${pipe.last_expired}</span></div>` : ''}
      ${(pipe.feedback?.learned_patterns ?? 0) > 0 ? `<div class="status-row"><span>Learned noise</span><span>${pipe.feedback.learned_patterns} patterns</span></div>` : ''}
      ${err}`;

    renderFinnhubStatus(pipe.finnhub || data.finnhub);
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

document.getElementById('test-finnhub').addEventListener('click', async () => {
  const btn = document.getElementById('test-finnhub');
  btn.disabled = true;
  btn.textContent = 'Testing…';
  try {
    const res = await api('/finnhub/test');
    renderFinnhubStatus(res);
    if (res.error) {
      showToast(res.error, 'error');
    } else {
      showToast('Finnhub connected', 'success');
    }
    // Refresh config row
    const cfg = await api('/settings');
    const items = [
      { label: 'Dry run', on: cfg.config?.dry_run },
      { label: 'Anthropic', on: cfg.config?.anthropic_configured },
      { label: 'X API', on: cfg.config?.x_configured },
      { label: 'Finnhub', on: cfg.config?.finnhub_configured },
    ];
    document.getElementById('config-info').innerHTML = items.map((i) => `
      <div class="status-row">
        <span>${esc(i.label)}</span>
        <span class="${i.on ? 'status-ok' : 'status-no'}">${i.on ? 'On' : 'Off'}</span>
      </div>`).join('');
  } catch (err) {
    showToast(err.message, 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = 'Test Finnhub connection';
  }
});

document.getElementById('fetch-now').addEventListener('click', async () => {
  const btn = document.getElementById('fetch-now');
  btn.disabled = true;
  btn.textContent = 'Fetching…';
  try {
    const res = await api('/pipeline/run', { method: 'POST' });
    const parts = [];
    if (res.last_ingest_count) parts.push(`${res.last_ingest_count} new headline${res.last_ingest_count === 1 ? '' : 's'}`);
    if (res.last_drafts_created) parts.push(`${res.last_drafts_created} draft${res.last_drafts_created === 1 ? '' : 's'}`);
    if (res.last_expired) parts.push(`${res.last_expired} expired`);
    showToast(parts.length ? parts.join(', ') : 'Fetch complete — no new stories', 'success');
    loadSettings();
    if (currentTab === 'queue') loadQueue();
  } catch (err) {
    showToast(err.message, 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = 'Fetch news now';
  }
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
