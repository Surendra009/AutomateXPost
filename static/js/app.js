/* PostPilot PWA — vanilla JS, no build step */

const API = '/api';
let currentTab = 'stock';
let refreshTimer = null;
let editingDraftId = null;
let rejectDraftId = null;
let scheduleDraftId = null;
let rejectionReasons = [];
let watchlist = [];
let searchTopics = [];
let queueData = { drafts: [], counts: { stock: 0, politics: 0 }, hidden_duplicates: 0 };

// ── API helpers ──────────────────────────────────────────

async function api(path, opts = {}, timeoutMs = 60000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  let res;
  try {
    res = await fetch(`${API}${path}`, {
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', ...opts.headers },
      signal: controller.signal,
      ...opts,
    });
  } catch (err) {
    if (err && err.name === 'AbortError') {
      throw new Error('Request timed out — server may be busy, try again');
    }
    throw new Error('Network error — check your connection and try again');
  } finally {
    clearTimeout(timer);
  }
  if (res.status === 401) {
    showLogin();
    throw new Error('Unauthorized');
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `Error ${res.status}`);
  return data;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function waitForPipelineDone(maxMs = 300000) {
  const start = Date.now();
  while (Date.now() - start < maxMs) {
    await sleep(2000);
    const status = await api('/pipeline/status');
    if (!status.running) return status;
  }
  throw new Error('Fetch is still running — check Activity in a minute');
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
    await api('/me', {}, 15000);
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
    const titles = {
      stock: 'Stock',
      politics: 'Politics',
      history: 'History',
      chat: 'Chat',
      settings: 'Settings',
    };
    const subtitles = {
      stock: 'Earnings, tickers & markets',
      politics: 'Geopolitics & policy',
      history: 'Posted & rejected',
      chat: 'Search drafts & topics',
      settings: 'Pipeline & limits',
    };
    document.getElementById('screen-title').textContent = titles[currentTab];
    document.getElementById('header-subtitle').textContent = subtitles[currentTab];
    loadCurrentTab();
  });
});

function loadCurrentTab() {
  if (currentTab === 'stock' || currentTab === 'politics') loadQueue();
  else if (currentTab === 'history') loadHistory();
  else if (currentTab === 'chat') initChatScreen();
  else if (currentTab === 'settings') loadSettings();
}

function isQueueTab() {
  return currentTab === 'stock' || currentTab === 'politics';
}

function startRefresh() {
  stopRefresh();
  refreshTimer = setInterval(() => {
    if (isQueueTab()) loadQueue(true);
  }, 30000);
}

function stopRefresh() {
  if (refreshTimer) clearInterval(refreshTimer);
}

// ── Chat assistant ───────────────────────────────────────

let chatBootstrapped = false;

function initChatScreen() {
  if (chatBootstrapped) return;
  chatBootstrapped = true;

  api('/settings').then((data) => {
    const chat = data.chat || {};
    const note = document.getElementById('chat-model-note');
    if (note && chat.provider && chat.provider !== 'none') {
      note.textContent = `Powered by ${chat.provider} · ${chat.model}`;
    } else if (note) {
      note.textContent = 'No LLM configured — results still work; add ANTHROPIC_API_KEY or OPENAI_API_KEY for summaries.';
    }
  }).catch(() => {});

  document.getElementById('chat-send').addEventListener('click', () => sendChatMessage());
  document.getElementById('chat-input').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendChatMessage();
    }
  });

  document.querySelectorAll('.chat-chip').forEach((btn) => {
    btn.addEventListener('click', () => {
      document.getElementById('chat-input').value = btn.dataset.query || '';
      document.getElementById('chat-live-news').checked = btn.dataset.news === '1';
      sendChatMessage();
    });
  });
}

function appendChatBubble(role, text, isLoading = false, data = null) {
  const thread = document.getElementById('chat-thread');
  const welcome = thread.querySelector('.chat-welcome');
  if (welcome) welcome.remove();

  const bubble = document.createElement('div');
  bubble.className = `chat-bubble ${role}${isLoading ? ' is-loading' : ''}`;

  if (role === 'assistant' && data && !isLoading) {
    const replyText = document.createElement('div');
    replyText.textContent = text;
    bubble.appendChild(replyText);
    const results = renderChatResults(data);
    if (results) bubble.appendChild(results);
  } else {
    bubble.textContent = text;
  }

  thread.appendChild(bubble);
  thread.scrollTop = thread.scrollHeight;
  return bubble;
}

function renderChatResults(data) {
  const wrap = document.createElement('div');
  wrap.className = 'chat-results';
  let hasResults = false;

  const addSection = (items, renderItem) => {
    items.forEach((item) => {
      wrap.appendChild(renderItem(item));
      hasResults = true;
    });
  };

  addSection(data.drafts || [], (d) => {
    const el = document.createElement('div');
    el.className = 'chat-result';
    el.dataset.lane = d.lane || (d.category === 'geopolitics' ? 'politics' : 'stock');
    const status = d.status === 'pending' ? 'In queue' : d.status;
    el.innerHTML = `
      <div class="chat-result-head">
        <span>Draft · ${esc(status)}</span>
        <span>${esc(d.age)}</span>
      </div>
      <p>${esc(d.text)}</p>
      <div class="chat-result-actions">
        ${d.status === 'pending' || d.status === 'scheduled'
          ? `<button type="button" class="btn btn-secondary" data-open-queue="${d.id}">Open queue</button>`
          : ''}
        ${d.headline?.url
          ? `<a class="btn btn-secondary" href="${esc(d.headline.url)}" target="_blank" rel="noopener">Source</a>`
          : ''}
      </div>`;
    el.querySelector('[data-open-queue]')?.addEventListener('click', (e) => {
      const lane = e.target.closest('.chat-result')?.dataset?.lane || 'stock';
      document.querySelector(`.tab[data-tab="${lane}"]`)?.click();
    });
    return el;
  });

  addSection(data.posted || [], (d) => {
    const el = document.createElement('div');
    el.className = 'chat-result';
    el.innerHTML = `
      <div class="chat-result-head"><span>Posted</span><span>${esc(d.age)}</span></div>
      <p>${esc(d.text)}</p>`;
    return el;
  });

  addSection(data.headlines || [], (h) => {
    const el = document.createElement('div');
    el.className = 'chat-result';
    el.innerHTML = `
      <div class="chat-result-head">
        <span>${esc(h.source)}</span>
        <span>${esc(h.age)}</span>
      </div>
      <p><a href="${esc(h.url)}" target="_blank" rel="noopener">${esc(h.title)}</a></p>`;
    return el;
  });

  addSection(data.earnings || [], (e) => {
    const el = document.createElement('div');
    el.className = 'chat-result';
    el.innerHTML = `
      <div class="chat-result-head"><span>Earnings · ${esc(e.source || 'Finnhub')}</span><span>${esc(e.date || '')}</span></div>
      <p>${esc(e.label || e.symbol)}</p>`;
    return el;
  });

  addSection(data.news || [], (n) => {
    const el = document.createElement('div');
    el.className = 'chat-result';
    el.innerHTML = `
      <div class="chat-result-head"><span>Live news</span><span>${esc(n.source || 'Web')}</span></div>
      <p><a href="${esc(n.url)}" target="_blank" rel="noopener">${esc(n.title)}</a></p>
      <div class="chat-result-actions">
        <button type="button" class="btn btn-secondary" data-track-topic="${esc(data.query)}">Track topic</button>
      </div>`;
    el.querySelector('[data-track-topic]')?.addEventListener('click', (e) => {
      addTopicFromChat(e.target.dataset.trackTopic);
    });
    return el;
  });

  return hasResults ? wrap : null;
}

async function addTopicFromChat(topic) {
  if (!topic) return;
  try {
    const settings = await api('/settings');
    const topics = settings.search_topics || [];
    const key = topic.toLowerCase();
    if (topics.some((t) => t.toLowerCase() === key)) {
      showToast('Topic already tracked', 'success');
      return;
    }
    topics.push(topic);
    await api('/settings', {
      method: 'PATCH',
      body: JSON.stringify({ search_topics: topics }),
    });
    showToast(`Added "${topic}" to Topics`, 'success');
  } catch (err) {
    showToast(err.message, 'error');
  }
}

async function sendChatMessage() {
  const input = document.getElementById('chat-input');
  const message = input.value.trim();
  if (!message) return;

  const fetchNews = document.getElementById('chat-live-news').checked;
  const sendBtn = document.getElementById('chat-send');
  input.value = '';
  sendBtn.disabled = true;

  appendChatBubble('user', message);
  const loadingBubble = appendChatBubble('assistant', 'Searching…', true);

  try {
    const data = await api('/chat', {
      method: 'POST',
      body: JSON.stringify({ message, fetch_news: fetchNews }),
    });
    loadingBubble.remove();
    appendChatBubble('assistant', data.reply, false, data);
  } catch (err) {
    loadingBubble.textContent = err.message || 'Search failed';
    loadingBubble.classList.remove('is-loading');
  } finally {
    sendBtn.disabled = false;
    input.focus();
  }
}

// ── Queue (stock & politics lanes) ───────────────────────

async function loadQueue(silent = false) {
  try {
    const data = await api('/queue');
    queueData = {
      drafts: data.drafts || [],
      counts: data.counts || { stock: 0, politics: 0 },
      hidden_duplicates: data.hidden_duplicates || 0,
    };
    rejectionReasons = data.rejection_reasons || rejectionReasons;
    if (isQueueTab()) renderLane(currentTab);
    updateBadges();
  } catch (err) {
    if (!silent) showToast(err.message, 'error');
  }
}

function renderLane(lane) {
  const drafts = queueData.drafts.filter((d) => d.lane === lane);
  renderQueue(drafts, lane);
  const sub = document.getElementById('header-subtitle');
  const count = drafts.length;
  if (queueData.hidden_duplicates > 0) {
    sub.textContent = `${count} draft${count === 1 ? '' : 's'} · ${queueData.hidden_duplicates} duplicate${queueData.hidden_duplicates === 1 ? '' : 's'} hidden`;
  } else {
    sub.textContent = count
      ? `${count} draft${count === 1 ? '' : 's'} waiting`
      : (lane === 'politics' ? 'Geopolitics & policy' : 'Earnings, tickers & markets');
  }
}

function updateBadges() {
  const counts = queueData.counts || { stock: 0, politics: 0 };
  const headerBadge = document.getElementById('queue-badge');
  const stockBadge = document.getElementById('stock-badge');
  const politicsBadge = document.getElementById('politics-badge');

  [['stock', stockBadge], ['politics', politicsBadge]].forEach(([lane, el]) => {
    const n = counts[lane] || 0;
    if (!el) return;
    if (n > 0) {
      el.textContent = n;
      el.classList.remove('hidden');
    } else {
      el.classList.add('hidden');
    }
  });

  const activeCount = counts[currentTab] || 0;
  if (isQueueTab() && activeCount > 0) {
    headerBadge.textContent = activeCount;
    headerBadge.classList.remove('hidden');
  } else {
    headerBadge.classList.add('hidden');
  }
}

function renderQueue(drafts, lane) {
  const list = document.getElementById(`${lane}-list`);
  const empty = document.getElementById(`${lane}-empty`);
  const sorted = [...drafts].sort(
    (a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime(),
  );
  if (!sorted.length) {
    list.innerHTML = '';
    empty.classList.remove('hidden');
    return;
  }
  empty.classList.add('hidden');
  list.innerHTML = sorted.map((d) => renderDraftCard(d)).join('');
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
          <div class="post-actions-row three-col">
            <button class="btn btn-secondary" data-action="schedule" data-id="${d.id}">Schedule</button>
            <button class="btn btn-edit" data-action="regenerate" data-id="${d.id}">Rewrite</button>
            <button class="btn btn-edit" data-action="edit" data-id="${d.id}">Edit</button>
          </div>
          <div class="post-actions-row">
            <button class="btn btn-reject" data-action="reject" data-id="${d.id}">Reject</button>
          </div>
        </div>
      </div>`;
  }

  const isEarnings = d.category === 'earnings';
  const draftMins = d.draft_age_minutes ?? 0;
  const draftStaleMins = isEarnings ? 120 : 420;
  const draftAgingMins = isEarnings ? 90 : 360;
  const draftAgeClass = draftMins >= draftStaleMins ? 'is-stale' : draftMins >= draftAgingMins ? 'is-aging' : '';
  const storyMins = d.story_age_minutes ?? 0;
  const storyAgingMins = isEarnings ? 60 : 180;
  const storyAgeClass = !d.story_fresh ? 'is-stale-story' : storyMins >= storyAgingMins ? 'is-aging-story' : '';
  const scheduledBadge = d.status === 'scheduled' && d.scheduled_at
    ? `<span class="scheduled-badge">Scheduled ${formatDate(d.scheduled_at)}</span>` : '';
  const postError = d.post_error
    ? `<div class="post-error">${esc(d.post_error)}</div>` : '';

  return `
    <article class="post-card" data-id="${d.id}" data-impact="${esc(d.impact)}">
      <div class="post-head">
        <span class="post-source">${source}${d.is_seed ? ' <span class="seed-badge">Sample — not live news</span>' : ''}${scheduledBadge}</span>
        <div class="post-head-actions">
          <button type="button" class="btn-icon" data-action="copy" data-id="${d.id}" title="Copy text" aria-label="Copy draft text">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/></svg>
          </button>
          <div class="post-age-wrap">
            <span class="post-age ${draftAgeClass}" title="Draft created ${esc(d.age)} ago">
              <span class="post-age-label">Draft</span> ${esc(d.age)}
            </span>
            ${d.story_age ? `<div class="story-age ${storyAgeClass}"><span class="post-age-label">Story</span> ${esc(d.story_age)}${!d.story_fresh ? ' · stale' : ''}</div>` : ''}
          </div>
        </div>
      </div>
      ${postError}
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
    rejectDraftId = id;
    openRejectModal();
  } else if (action === 'regenerate') {
    btn.disabled = true;
    try {
      await api(`/drafts/${id}/regenerate`, { method: 'POST' }, 120000);
      showToast('New draft generated', 'success');
      loadQueue();
    } catch (err) {
      showToast(err.message, 'error');
      btn.disabled = false;
    }
  } else if (action === 'schedule') {
    scheduleDraftId = id;
    openScheduleModal();
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

function bindPullRefresh(screenId, indicatorId) {
  const screen = document.getElementById(screenId);
  if (!screen) return;
  screen.addEventListener('touchstart', (e) => {
    touchStartY = e.touches[0].clientY;
  }, { passive: true });
  screen.addEventListener('touchmove', (e) => {
    const diff = e.touches[0].clientY - touchStartY;
    if (diff > 60 && window.scrollY === 0) {
      document.getElementById(indicatorId)?.classList.remove('hidden');
    }
  }, { passive: true });
  screen.addEventListener('touchend', async () => {
    const indicator = document.getElementById(indicatorId);
    if (!indicator || indicator.classList.contains('hidden')) return;
    indicator.classList.add('hidden');
    await loadQueue();
    showToast('Refreshed', 'success');
  });
}

bindPullRefresh('stock-screen', 'stock-pull-indicator');
bindPullRefresh('politics-screen', 'politics-pull-indicator');

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

  const analytics = data.analytics || {};
  const banner = document.getElementById('analytics-banner');
  if (analytics.totals && analytics.totals.count) {
    banner.classList.remove('hidden');
    document.getElementById('analytics-totals').textContent =
      `${analytics.totals.likes || 0} likes · ${analytics.totals.retweets || 0} RTs`;
  } else {
    banner.classList.add('hidden');
  }

  const postedList = document.getElementById('posted-list');
  const postedEmpty = document.getElementById('posted-empty');
  if (data.posted.length) {
    postedEmpty.classList.add('hidden');
    postedList.innerHTML = data.posted.map((p) => `
      <div class="list-item">
        <p>${esc(p.text)}</p>
        <div class="list-item-foot">
          <span>${formatDate(p.posted_at)}</span>
          ${(p.likes || p.retweets) ? `<span class="engagement-pill">♥ ${p.likes || 0} · ↻ ${p.retweets || 0}</span>` : ''}
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

async function loadSettings() {
  try {
    const data = await api('/settings');
    document.getElementById('pipeline-toggle').checked = data.pipeline_enabled;
    document.getElementById('daily-cap').value = data.daily_post_cap;
    document.getElementById('cooldown').value = data.cooldown_minutes;
    document.getElementById('push-enabled').checked = data.push_enabled !== false;
    document.getElementById('discord-enabled').checked = data.discord_enabled !== false;
    const discordDesc = document.getElementById('discord-status-desc');
    const discordOk = data.discord?.configured || data.config?.discord_configured;
    if (discordDesc) {
      discordDesc.textContent = discordOk
        ? 'Post new drafts to your Discord channel'
        : 'Set DISCORD_WEBHOOK_URL on Railway, then redeploy';
    }
    const testDiscordBtn = document.getElementById('test-discord');
    if (testDiscordBtn) {
      testDiscordBtn.disabled = !discordOk;
    }
    watchlist = data.watchlist || [];
    renderWatchlist();
    searchTopics = data.search_topics || [];
    renderTopics();

    const pauseEl = document.getElementById('pause-status');
    if (data.paused_until) {
      pauseEl.textContent = `Paused until ${formatDate(data.paused_until)}`;
      pauseEl.classList.remove('hidden');
    } else {
      pauseEl.textContent = '';
      pauseEl.classList.add('hidden');
    }

    const pipe = data.pipeline || {};
    const cfg = data.config || {};
    const sched = pipe.schedule || {};
    const lastRun = pipe.last_run_at ? formatDate(pipe.last_run_at) : 'Never';
    let schedLabel;
    if (sched.quiet_hours) {
      schedLabel = `Quiet (${sched.quiet_window || '10pm–5am'})`;
    } else if (sched.earnings_window) {
      schedLabel = `Earnings window (${sched.pipeline_interval_seconds || 120}s)`;
    } else if (sched.market_hours) {
      schedLabel = `Market hours (${sched.pipeline_interval_seconds || 120}s)`;
    } else if (sched.is_weekend) {
      schedLabel = `Weekend (every ${sched.weekend_interval_hours || 3}h)`;
    } else {
      schedLabel = 'Active';
    }
    const err = pipe.last_error ? `<div class="pipeline-error">${esc(pipe.last_error)}</div>` : '';
    const earn = pipe.earnings || {};
    let earnLine = '';
    if (earn.configured === false) {
      earnLine = '<div class="status-row"><span>Earnings</span><span class="status-no">Add FINNHUB_KEY</span></div>';
    } else if (earn.reporting_today > 0) {
      earnLine = `<div class="status-row"><span>Earnings today</span><span>${earn.reporting_today} on watchlist</span></div>`;
    }
    document.getElementById('pipeline-status').innerHTML = `
      <div class="status-row">
        <span>Schedule</span>
        <span>${esc(schedLabel)}</span>
      </div>
      <div class="status-row">
        <span>Last fetch</span>
        <span>${esc(lastRun)}</span>
      </div>
      <div class="status-row">
        <span>Last cycle</span>
        <span>${pipe.last_drafts_created ?? 0} drafts · ${pipe.last_ingest_count ?? 0} headlines</span>
      </div>
      ${earnLine}
      <div class="status-row">
        <span>Build</span>
        <span>${esc(cfg.build || '—')}</span>
      </div>
      <div class="status-row">
        <span>Draft LLM</span>
        <span>${esc((data.llm?.draft_provider || '—') + ' / ' + (data.llm?.draft_model || '—'))}</span>
      </div>
      <div class="status-row">
        <span>Finnhub</span>
        <span class="${cfg.finnhub_configured ? 'status-ok' : 'status-no'}">${cfg.finnhub_configured ? 'Connected' : 'Not set'}</span>
      </div>
      ${err}`;
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
    btn.addEventListener('click', async () => {
      watchlist = watchlist.filter((t) => t !== btn.dataset.ticker);
      renderWatchlist();
      await persistWatchlist();
    });
  });
}

async function persistWatchlist() {
  try {
    const data = await api('/settings', {
      method: 'PATCH',
      body: JSON.stringify({ watchlist }),
    });
    watchlist = data.watchlist || watchlist;
    renderWatchlist();
  } catch (err) {
    showToast(err.message, 'error');
  }
}

async function persistSearchTopics() {
  try {
    const data = await api('/settings', {
      method: 'PATCH',
      body: JSON.stringify({ search_topics: searchTopics }),
    });
    searchTopics = data.search_topics || searchTopics;
    renderTopics();
  } catch (err) {
    showToast(err.message, 'error');
  }
}

async function addWatchlistTicker(raw) {
  const val = raw.trim().toUpperCase().replace(/^\$/, '');
  if (!val || watchlist.includes(val)) return false;
  watchlist.push(val);
  renderWatchlist();
  await persistWatchlist();
  return true;
}

document.getElementById('add-ticker').addEventListener('click', async () => {
  const input = document.getElementById('watchlist-input');
  if (await addWatchlistTicker(input.value)) {
    input.value = '';
  }
});

document.getElementById('watchlist-input').addEventListener('keydown', async (e) => {
  if (e.key !== 'Enter') return;
  e.preventDefault();
  const input = e.target;
  if (await addWatchlistTicker(input.value)) {
    input.value = '';
  }
});

function renderTopics() {
  const container = document.getElementById('topic-chips');
  if (!container) return;
  container.innerHTML = searchTopics.map((topic) => `
    <span class="chip">${esc(topic)}
      <button class="chip-remove" data-topic="${esc(topic)}">&times;</button>
    </span>`).join('');
  container.querySelectorAll('.chip-remove').forEach((btn) => {
    btn.addEventListener('click', async () => {
      searchTopics = searchTopics.filter((t) => t !== btn.dataset.topic);
      renderTopics();
      await persistSearchTopics();
    });
  });
}

async function addSearchTopic(raw) {
  const val = raw.trim().replace(/\s+/g, ' ');
  if (!val) return false;
  const key = val.toLowerCase();
  if (searchTopics.some((t) => t.toLowerCase() === key)) return false;
  searchTopics.push(val);
  renderTopics();
  await persistSearchTopics();
  return true;
}

document.getElementById('add-topic').addEventListener('click', async () => {
  const input = document.getElementById('topic-input');
  if (await addSearchTopic(input.value)) {
    input.value = '';
  }
});

document.getElementById('topic-input').addEventListener('keydown', async (e) => {
  if (e.key !== 'Enter') return;
  e.preventDefault();
  const input = e.target;
  if (await addSearchTopic(input.value)) {
    input.value = '';
  }
});

document.getElementById('fetch-now').addEventListener('click', async () => {
  const btn = document.getElementById('fetch-now');
  btn.disabled = true;
  btn.textContent = 'Fetching…';
  try {
    const start = await api('/pipeline/run', { method: 'POST' });
    if (!start.started) {
      showToast('Pipeline is already running', 'error');
      return;
    }
    const res = await waitForPipelineDone();
    if (res.last_error) {
      showToast(`Fetch failed: ${res.last_error}`, 'error');
      loadSettings();
      return;
    }
    const ingested = res.last_ingest_count || 0;
    const drafts = res.last_drafts_created || 0;
    const filtered = res.last_filter_kept || 0;
    const parts = [];
    if (ingested) parts.push(`${ingested} new headline${ingested === 1 ? '' : 's'}`);
    if (filtered) parts.push(`${filtered} passed filter`);
    if (drafts) parts.push(`${drafts} new draft${drafts === 1 ? '' : 's'}`);
    if (res.last_expired) parts.push(`${res.last_expired} expired`);
    if (!ingested && !drafts) {
      showToast('No new stories — add tickers to watchlist or try again later.', 'success');
    } else {
      showToast(parts.length ? parts.join(', ') : 'Fetch complete', 'success');
    }
    loadSettings();
    if (isQueueTab()) loadQueue();
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
        push_enabled: document.getElementById('push-enabled').checked,
        discord_enabled: document.getElementById('discord-enabled').checked,
        watchlist,
        search_topics: searchTopics,
      }),
    });
    showToast('Settings saved', 'success');
  } catch (err) {
    showToast(err.message, 'error');
  }
});

document.getElementById('resume-pipeline').addEventListener('click', async () => {
  try {
    await api('/settings', {
      method: 'PATCH',
      body: JSON.stringify({ paused_until: '' }),
    });
    showToast('Pipeline resumed', 'success');
    loadSettings();
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

// ── Reject / Schedule modals ─────────────────────────────

const REJECT_LABELS = {
  too_vague: 'Too vague',
  too_small: 'Too small / thin',
  wrong_ticker: 'Wrong ticker',
  bad_hook: 'Weak hook',
  too_long: 'Too long',
  duplicate: 'Duplicate',
  off_topic: 'Off topic',
  listicle: 'Listicle / fluff',
  other: 'Other',
};

function openRejectModal() {
  const grid = document.getElementById('reject-reasons');
  const reasons = rejectionReasons.length ? rejectionReasons : Object.keys(REJECT_LABELS);
  grid.innerHTML = reasons.map((r) => `
    <button type="button" class="reason-btn" data-reason="${esc(r)}">${esc(REJECT_LABELS[r] || r)}</button>
  `).join('');
  grid.querySelectorAll('.reason-btn').forEach((btn) => {
    btn.addEventListener('click', () => submitReject(btn.dataset.reason));
  });
  document.getElementById('reject-modal').classList.remove('hidden');
}

async function submitReject(reason) {
  document.getElementById('reject-modal').classList.add('hidden');
  if (!rejectDraftId) return;
  try {
    await api(`/drafts/${rejectDraftId}/reject`, {
      method: 'POST',
      body: JSON.stringify({ reason }),
    });
    showToast('Rejected — will improve future drafts', 'success');
    loadQueue();
  } catch (err) {
    showToast(err.message, 'error');
  }
  rejectDraftId = null;
}

document.getElementById('reject-cancel').addEventListener('click', () => {
  document.getElementById('reject-modal').classList.add('hidden');
  rejectDraftId = null;
});

function openScheduleModal() {
  const input = document.getElementById('schedule-input');
  const d = new Date(Date.now() + 30 * 60000);
  input.value = toLocalDatetimeValue(d);
  document.getElementById('schedule-modal').classList.remove('hidden');
}

function toLocalDatetimeValue(d) {
  const pad = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

document.querySelectorAll('.schedule-quick-btn').forEach((btn) => {
  btn.addEventListener('click', () => {
    const mins = parseInt(btn.dataset.minutes, 10);
    const d = new Date(Date.now() + mins * 60000);
    document.getElementById('schedule-input').value = toLocalDatetimeValue(d);
  });
});

document.getElementById('schedule-cancel').addEventListener('click', () => {
  document.getElementById('schedule-modal').classList.add('hidden');
  scheduleDraftId = null;
});

document.getElementById('schedule-confirm').addEventListener('click', async () => {
  const val = document.getElementById('schedule-input').value;
  if (!val || !scheduleDraftId) return;
  const scheduled_at = new Date(val).toISOString();
  try {
    await api(`/drafts/${scheduleDraftId}/approve`, {
      method: 'POST',
      body: JSON.stringify({ scheduled_at }),
    });
    showToast('Scheduled', 'success');
    document.getElementById('schedule-modal').classList.add('hidden');
    scheduleDraftId = null;
    loadQueue();
  } catch (err) {
    showToast(err.message, 'error');
  }
});

// ── Push notifications ───────────────────────────────────

function urlBase64ToUint8Array(base64String) {
  const padding = '='.repeat((4 - (base64String.length % 4)) % 4);
  const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
  const raw = atob(base64);
  return Uint8Array.from([...raw].map((c) => c.charCodeAt(0)));
}

async function subscribePush() {
  if (!('serviceWorker' in navigator) || !('PushManager' in window)) {
    showToast('Push not supported on this browser', 'error');
    return;
  }
  try {
    const { public_key } = await api('/push/vapid-public-key');
    const reg = await navigator.serviceWorker.ready;
    const sub = await reg.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: urlBase64ToUint8Array(public_key),
    });
    const json = sub.toJSON();
    await api('/push/subscribe', {
      method: 'POST',
      body: JSON.stringify({
        endpoint: json.endpoint,
        keys: json.keys,
      }),
    });
    showToast('Push enabled', 'success');
  } catch (err) {
    showToast(err.message || 'Push setup failed', 'error');
  }
}

document.getElementById('test-discord').addEventListener('click', async () => {
  const btn = document.getElementById('test-discord');
  btn.disabled = true;
  btn.textContent = 'Sending…';
  try {
    await api('/discord/test', { method: 'POST' });
    showToast('Test message sent to Discord', 'success');
  } catch (err) {
    showToast(err.message, 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = 'Test Discord connection';
  }
});

document.getElementById('enable-push').addEventListener('click', subscribePush);

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
  navigator.serviceWorker.register('/sw.js?v=50').catch(() => {});
}

// ── Init ─────────────────────────────────────────────────

try {
  checkAuth();
} catch {
  showLogin();
}
