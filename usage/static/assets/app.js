'use strict';

const $ = id => document.getElementById(id);
const integer = new Intl.NumberFormat('en-US', { maximumFractionDigits: 0 });
const compact = new Intl.NumberFormat('en-US', { notation: 'compact', maximumFractionDigits: 1 });
const decimal = new Intl.NumberFormat('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 4 });
const percent = new Intl.NumberFormat('en-US', { maximumFractionDigits: 2 });
const valid = Number.isFinite;
const total = (rows, key) => rows.reduce((sum, row) => sum + (valid(row[key]) ? row[key] : 0), 0);
const count = value => valid(value) ? integer.format(value) : '—';
const money = value => !valid(value) ? '—' : value > 0 && value < .0001 ? '< 0.0001' : decimal.format(value);
const costLabel = value => !valid(value) ? '—' : value > 0 && value < .0001 ? '< $0.0001' : '$' + value.toFixed(value > 0 && value < .01 ? 4 : 2);
const confirmed = row => row.status === 'completed' && row.duration_exact && valid(row.duration_ms);
const stamp = value => new Date(value * 1000).toLocaleString('en-US', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
const color = name => getComputedStyle(document.documentElement).getPropertyValue('--' + name).trim();
const charts = {};
// Agent metadata comes from the backend registry; nothing here names a specific agent.
const agents = new Map();
let chartSignature = '', activitySignature = '', limitSignature = '', heatmapSignature = '';
let data = null, accounts = [], failed = false, limitsLoaded = false, topBar = false;
let buckets = new Map(), bucketId = null, agentFilter = 'all', promptId = null;
const feeds = { usage: { seconds: 1 }, limits: { seconds: 1 } };
const systemTheme = matchMedia('(prefers-color-scheme: dark)');
const preferences = { theme: 'dark', limitAlert: 0, period: 'today', style: 'minimal', pinned: false, indicator: 'bar', hiddenLimits: [], details: false };
const pending = new Map();
let sequence = 0;
const backendReady = new Promise(resolve => new QWebChannel(qt.webChannelTransport, channel => {
  const backend = channel.objects.backend;
  backend.layout_changed.connect((width, scale) => {
    const root = document.documentElement;
    root.style.setProperty('--layout-width', width + 'px');
    root.style.setProperty('--layout-scale', scale);
    root.dataset.narrow = String(width <= 360);
    requestAnimationFrame(reportSize);
  });
  backend.reply.connect((id, raw) => {
    const entry = pending.get(id); if (!entry) return;
    pending.delete(id);
    const result = JSON.parse(raw);
    result?.error ? entry.reject(Error(result.error)) : entry.resolve(result);
  });
  resolve(backend);
}));
async function native(method, args = null) {
  const backend = await backendReady, id = String(++sequence);
  return new Promise((resolve, reject) => { pending.set(id, { resolve, reject }); backend.request(id, method, JSON.stringify(args)); });
}
function settingsMessage(message = '') {
  $('settings-status').textContent = message; $('settings-status').hidden = !message;
}

function applyTheme() {
  document.documentElement.dataset.theme = preferences.theme === 'system' ? (systemTheme.matches ? 'dark' : 'light') : preferences.theme;
  document.documentElement.dataset.style = preferences.style;
  document.documentElement.dataset.indicator = preferences.indicator;
}

function savePreferences() {
  native('save_preferences', preferences).catch(error => settingsMessage(error.message));
}

function updatePeriod() {
  for (const button of $('period').children) button.setAttribute('aria-pressed', String(button.dataset.period === preferences.period));
}

applyTheme();
updatePeriod();

function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function duration(ms) {
  if (!valid(ms)) return '—';
  if (ms > 0 && ms < 1000) return '< 1 s';
  const seconds = Math.round(ms / 1000);
  return seconds < 60 ? seconds + ' s' : seconds < 3600 ? Math.floor(seconds / 60) + ' min ' + seconds % 60 + ' s'
    : Math.floor(seconds / 3600) + ' h ' + Math.floor(seconds % 3600 / 60) + ' min';
}

const agentName = id => agents.get(id)?.name ?? id;
const agentColor = id => agents.get(id)?.colors[document.documentElement.dataset.theme === 'dark' ? 1 : 0] || color('neutral');

function agentIcon(id) {
  const img = el('img', undefined, 'agent-icon'); img.alt = ''; img.width = 18; img.height = 18;
  img.src = agents.get(id)?.icon || 'assets/widget.svg';
  img.dataset.monochrome = String(agents.get(id)?.monochrome === true);
  return img;
}

function activeAccounts() {
  return accounts.filter(account => agents.has(account.id))
    .map(account => ({ ...account, windows: account.windows.filter(item => item.resets_at > Date.now() / 1000) }))
    .filter(account => account.windows.length);
}

function knownAgents() {
  const seen = new Set([...activeAccounts().map(account => account.id), ...(data?.rows || []).map(row => row.agent)]);
  return [...agents.keys()].filter(id => seen.has(id));
}

function weeklyWindows() {
  return activeAccounts().filter(account => agentFilter === 'all' || account.id === agentFilter).flatMap(account => {
    const item = account.windows.find(w => w.minutes === 10080 && w.resets_at - w.minutes * 60 <= Date.now() / 1000);
    return item ? [{ agent: account.id, start: item.resets_at - item.minutes * 60, end: item.resets_at }] : [];
  });
}

function updateAgents() {
  const known = knownAgents();
  if (agentFilter !== 'all' && !known.includes(agentFilter)) { agentFilter = 'all'; promptId = null; bucketId = null; }
  const tabs = $('agents');
  if (tabs.dataset.options !== JSON.stringify(known)) {
    tabs.replaceChildren();
    for (const id of known.length > 1 ? ['all', ...known] : []) {
      const button = el('button'); button.type = 'button'; button.dataset.agent = id;
      button.title = id === 'all' ? 'All agents' : agentName(id);
      button.setAttribute('aria-label', button.title);
      if (id === 'all') button.append(el('span', 'All'));
      else button.append(agentIcon(id));
      button.addEventListener('click', () => { agentFilter = id; promptId = null; bucketId = null; render(); });
      tabs.append(button);
    }
    tabs.dataset.options = JSON.stringify(known);
  }
  for (const button of tabs.children) button.setAttribute('aria-pressed', String(button.dataset.agent === agentFilter));
  tabs.hidden = known.length < 2;
  $('dashboard-empty').hidden = known.length > 0;
  $('dashboard-empty').textContent = (data || failed) && limitsLoaded ? 'No agent usage yet.' : 'Loading…';
}

function selected() {
  const rows = (data?.rows || []).filter(row => agentFilter === 'all' || row.agent === agentFilter);
  const start = new Date(); start.setHours(0, 0, 0, 0);
  if (preferences.period === 'weekly') {
    const windows = weeklyWindows();
    return rows.filter(row => row.start <= Date.now() / 1000 && windows.some(w => w.agent === row.agent && row.start >= w.start && row.start < w.end));
  }
  const cutoff = preferences.period === 'all' ? 0 : start.getTime() / 1000;
  return rows.filter(row => row.start >= cutoff && row.start <= Date.now() / 1000);
}

function promptLabel(row, limit = 64) {
  const title = row?.title || 'Untitled prompt';
  return title.length > limit ? title.slice(0, limit - 1).trimEnd() + '…' : title;
}

function dayKey(date) {
  return [date.getFullYear(), String(date.getMonth() + 1).padStart(2, '0'), String(date.getDate()).padStart(2, '0')].join('-');
}

function shiftDay(date, offset) {
  const next = new Date(date); next.setDate(next.getDate() + offset); return next;
}

function bucketKey(start, agent = '') {
  return (agent ? agent + ':' : '') + dayKey(new Date(start * 1000));
}

function bucketSummary(bucket) {
  const prompts = count(bucket.prompts) + (bucket.prompts === 1 ? ' prompt' : ' prompts');
  return !bucket.prompts ? 'No prompts' : prompts + (bucket.missing ? ' · ' + count(bucket.missing) + ' without tokens' : '');
}

function hideHeatmapTooltip() {
  $('heatmap-tooltip').hidden = true;
  $('heatmaps').querySelector('[aria-describedby]')?.removeAttribute('aria-describedby');
}

function showHeatmapTooltip(cell) {
  const bucket = buckets.get(cell?.dataset.key);
  hideHeatmapTooltip();
  if (!bucket || cell.disabled) return;
  const popup = $('heatmap-tooltip');
  popup.replaceChildren(el('p', bucket.label, 'tooltip-date'));
  if (bucket.prompts) popup.append(el('strong', bucket.known ? compact.format(bucket.tokens) + ' tokens' : 'Tokens unavailable'));
  popup.append(el('p', bucketSummary(bucket), 'tooltip-summary'));
  if (!bucket.agent && agentFilter === 'all') {
    for (const agent of [...new Set(bucket.rows.map(row => row.agent))]) {
      const rows = bucket.rows.filter(row => row.agent === agent), known = rows.filter(row => valid(row.tokens));
      const detail = el('p', undefined, 'tooltip-agent');
      detail.append(agentIcon(agent), el('span', agentName(agent) + ' · '
        + (known.length ? compact.format(total(known, 'tokens')) + ' tokens' : 'Tokens unavailable')));
      popup.append(detail);
    }
  }
  popup.hidden = false;
  cell.setAttribute('aria-describedby', popup.id);
  const rect = cell.getBoundingClientRect(), box = popup.getBoundingClientRect();
  popup.style.left = Math.max(8, Math.min(innerWidth - box.width - 8, rect.x + rect.width / 2 - box.width / 2)) + 'px';
  popup.style.top = (rect.top >= box.height + 16 ? rect.top - box.height - 10 : rect.bottom + 10) + 'px';
}

function renderHeatmaps(rows) {
  const period = preferences.period, weekly = period === 'weekly', now = new Date();
  const cycles = weekly ? weeklyWindows() : [{ agent: agentFilter }];
  const today = new Date(now); today.setHours(0, 0, 0, 0);
  const signature = JSON.stringify([period, cycles, dayKey(today), rows, document.documentElement.dataset.theme]);
  const host = $('heatmaps');
  if (signature !== heatmapSignature) {
    const previousMode = host.dataset.period;
    const scroller = host.querySelector('.heatmap-scroll');
    const scrollLeft = scroller?.scrollLeft || 0;
    const atEnd = !scroller || scroller.scrollWidth - scrollLeft - scroller.clientWidth < 4;
    const focused = host.contains(document.activeElement) ? document.activeElement.dataset.key : null;
    const hovered = host.querySelector('[aria-describedby]')?.dataset.key;
    heatmapSignature = signature; host.dataset.period = period;
    buckets = new Map();
    for (const row of rows) {
      const id = bucketKey(row.start, weekly ? row.agent : '');
      if (!buckets.has(id)) buckets.set(id, { rows: [], prompts: 0, known: 0, missing: 0, tokens: 0 });
      const bucket = buckets.get(id); bucket.prompts++; bucket.rows.push(row);
      if (valid(row.tokens)) { bucket.tokens += row.tokens; bucket.known++; }
      else bucket.missing++;
    }
    // Every strip shares one token scale, while each keeps its own reset window.
    const max = [...buckets.values()].reduce((value, bucket) => Math.max(value, bucket.tokens), 1);
    const maps = cycles.map(cycle => {
      const map = $('heatmap-template').content.firstElementChild.cloneNode(true);
      const part = name => map.querySelector('.heatmap-' + name);
      map.dataset.layout = weekly ? 'days' : 'weeks';
      if (cycle.agent !== 'all') map.style.setProperty('--heat', agentColor(cycle.agent));
      if (weekly) {
        part('heading').hidden = false;
        part('heading').append(agentIcon(cycle.agent), el('span', agentName(cycle.agent)));
        part('grid').setAttribute('aria-label', agentName(cycle.agent) + ' current weekly cycle. Recorded tokens per day.');
      }
      const monday = shiftDay(today, -(today.getDay() + 6) % 7);
      let start = weekly ? new Date(cycle.start * 1000) : shiftDay(monday, -21 * 7);
      start.setHours(0, 0, 0, 0);
      if (!weekly && rows.length) {
        const first = new Date(rows.reduce((first, row) => Math.min(first, row.start), Infinity) * 1000); first.setHours(0, 0, 0, 0);
        if (first < start) start = shiftDay(first, -(first.getDay() + 6) % 7);
      }
      const end = weekly ? new Date((cycle.end - 1) * 1000) : shiftDay(monday, 6);
      const dates = [];
      for (let day = start; day <= end; day = shiftDay(day, 1)) dates.push(day);
      map.style.setProperty('--rows', weekly ? 1 : 7);
      map.style.setProperty('--columns', weekly ? dates.length : dates.length / 7);
      part('labels').replaceChildren(...(weekly ? [] : ['M', '', 'W', '', 'F', '', 'S'].map(label => el('span', label))));
      const axis = [];
      if (weekly) {
        for (const day of dates) axis.push(el('span', day.toLocaleDateString('en-US', { weekday: 'short' }) + '\n' + day.getDate()));
      } else {
        let lastColumn = -3;
        for (const [index, day] of dates.entries()) {
          const column = Math.floor(index / 7);
          if ((!axis.length || day.getDate() === 1) && column - lastColumn >= 3) {
            const label = el('span', day.toLocaleDateString('en-US', { month: 'short' }));
            label.style.gridColumn = column + 1; axis.push(label); lastColumn = column;
          }
        }
      }
      part('axis').replaceChildren(...axis);
      for (const day of dates) {
        const id = bucketKey(day / 1000, weekly ? cycle.agent : '');
        const bucket = buckets.get(id) || { rows: [], prompts: 0, known: 0, missing: 0, tokens: 0 };
        bucket.agent = weekly ? cycle.agent : null;
        bucket.label = (weekly ? agentName(cycle.agent) + ' · ' : '')
          + day.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
        bucket.disabled = day > today;
        buckets.set(id, bucket);
        const cell = el('button', undefined, 'heat-cell'); cell.type = 'button'; cell.tabIndex = -1; cell.dataset.key = id;
        cell.dataset.level = bucket.tokens ? Math.ceil(bucket.tokens / max * 4) : 0;
        cell.dataset.unknown = String(bucket.prompts > 0 && !bucket.known);
        cell.disabled = bucket.disabled;
        cell.setAttribute('aria-label', bucket.label + '. ' + (bucket.known ? count(bucket.tokens) + ' recorded tokens. ' : '') + bucketSummary(bucket));
        part('grid').append(cell);
      }
      const format = date => date.toLocaleDateString('en-US', { month: 'short', day: 'numeric', ...(weekly ? {} : { year: 'numeric' }) });
      part('range').textContent = format(weekly ? new Date(cycle.start * 1000) : start) + ' – '
        + format(weekly ? new Date(cycle.end * 1000) : today);
      if (weekly) part('range').title = stamp(cycle.start) + ' – ' + stamp(cycle.end);
      return map;
    });
    hideHeatmapTooltip(); heatmapObserver.disconnect(); host.replaceChildren(...maps);
    for (const map of maps) {
      const scroller = map.querySelector('.heatmap-scroll');
      if (!weekly) scroller.scrollLeft = atEnd || previousMode !== period ? scroller.scrollWidth : scrollLeft;
      heatmapObserver.observe(map.querySelector('.heatmap-grid'));
    }
    const cells = [...host.querySelectorAll('.heat-cell')];
    if (focused) cells.find(cell => cell.dataset.key === focused)?.focus({ preventScroll: true });
    if (hovered && !focused) showHeatmapTooltip(cells.find(cell => cell.dataset.key === hovered));
  }
  if (!buckets.has(bucketId) || buckets.get(bucketId).disabled) bucketId = null;
  const key = bucketId || bucketKey(rows[0]?.start ?? now / 1000, weekly ? rows[0]?.agent || cycles[0].agent : '');
  for (const grid of host.querySelectorAll('.heatmap-grid')) {
    let selectedCell;
    for (const cell of grid.children) {
      const chosen = cell.dataset.key === key;
      cell.setAttribute('aria-pressed', String(chosen)); cell.tabIndex = chosen ? 0 : -1;
      if (chosen) selectedCell = cell;
    }
    if (!selectedCell) grid.querySelector('button:not(:disabled)')?.setAttribute('tabindex', '0');
  }
  return key;
}

function renderHistoryChart(rows, active) {
  const available = rows.some(row => valid(row.tokens));
  $('usage-chart').hidden = !available || typeof Chart === 'undefined';
  $('usage-empty').hidden = available && typeof Chart !== 'undefined';
  $('usage-empty').textContent = rows.length ? 'No token data' : 'No prompts';
  if (!available || typeof Chart === 'undefined') { chartSignature = ''; return; }
  const signature = JSON.stringify([rows.map(r => [r.id, r.agent, r.start, r.tokens, r.title]), active?.id, document.documentElement.dataset.theme, preferences.style]);
  if (signature === chartSignature) return;
  chartSignature = signature;
  const shown = [...new Set(rows.map(row => row.agent))];
  const firstTime = rows.reduce((first, row) => Math.min(first, row.start), Infinity), lastTime = Math.max(...rows.map(row => row.start));
  const datasets = shown.map(agent => {
    const tone = agentColor(agent);
    return {
      label: agentName(agent), data: rows.filter(row => row.agent === agent).slice().reverse().map(row => ({ x: row.start, y: row.tokens, id: row.id, title: row.title })),
      borderColor: tone, borderWidth: Number(color('chart-width')), fill: false, clip: false,
      tension: 0, spanGaps: false,
      pointRadius: context => context.raw?.id === active?.id ? 3.5 : 2.5,
      pointBorderWidth: 0,
      pointBackgroundColor: context => context.raw?.id === active?.id ? color('text') : tone,
      pointHoverRadius: 4,
      pointHitRadius: 8,
    };
  });
  Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
  const options = {
    responsive: true, maintainAspectRatio: false, animation: false,
    layout: { padding: { left: 3, right: 7, top: 6 } },
    interaction: { mode: 'nearest', intersect: true, axis: 'xy' },
    onClick: (event, elements) => {
      if (!elements.length) return;
      const point = elements[0]; promptId = datasets[point.datasetIndex].data[point.index].id;
      renderActivity();
    },
    plugins: {
      legend: { display: shown.length > 1, labels: { usePointStyle: true, boxWidth: 5, boxHeight: 5, font: { size: 9 }, color: color('muted') } },
      tooltip: {
        displayColors: false, backgroundColor: color('surface'), titleColor: color('muted'), bodyColor: color('text'),
        titleFont: { size: 9, weight: 'normal' }, bodyFont: { size: 11, weight: '600' },
        padding: 12, cornerRadius: Number(color('tooltip-radius')), caretSize: 3, caretPadding: 7, borderWidth: 0,
        callbacks: {
          title: items => promptLabel(items[0].raw, 32),
          label: item => compact.format(item.parsed.y) + ' tokens',
          footer: items => stamp(items[0].parsed.x) + (shown.length > 1 ? ' · ' + items[0].dataset.label : ''),
        },
        footerColor: color('muted'), footerFont: { size: 8, weight: 'normal' }, footerMarginTop: 5,
      },
    },
    scales: {
      x: { type: 'linear', min: firstTime === lastTime ? firstTime - 60 : firstTime, max: firstTime === lastTime ? lastTime + 60 : lastTime,
        grid: { display: false }, border: { display: false }, ticks: {
        maxTicksLimit: 3, maxRotation: 0, color: color('muted'), font: { size: 8 },
        callback: value => new Date(value * 1000).toLocaleString('en-US', lastTime - firstTime < 86400 ? { hour: '2-digit', minute: '2-digit' } : { month: 'short', day: 'numeric' }),
      } },
      y: { beginAtZero: true, border: { display: false }, grid: { color: color('line'), drawTicks: false }, ticks: {
        maxTicksLimit: 3, padding: 7, color: color('muted'), font: { size: 8 }, callback: value => compact.format(value),
      } },
    },
  };
  if (charts.usage) { charts.usage.data = { datasets }; charts.usage.options = options; charts.usage.update('none'); }
  else charts.usage = new Chart($('usage-chart'), { type: 'line', data: { datasets }, options });
}

function renderPrompt(row) {
  $('prompt-detail').hidden = !row;
  if (!row) return;
  $('prompt-cost').textContent = costLabel(row.cost);
  $('prompt-cost').title = valid(row.cost) ? '$' + money(row.cost) : '';
  $('prompt-tokens').textContent = valid(row.tokens) ? compact.format(row.tokens) : '—';
  $('prompt-tokens').title = count(row.tokens) + ' tokens';
  $('prompt-duration').textContent = confirmed(row) ? duration(row.duration_ms).replaceAll(' min ', 'm ').replaceAll(' s', 's').replaceAll(' h ', 'h ') : row.status === 'running' ? 'Running' : '—';
}

// A day is one summary line; its chart and prompts open below on request.
function renderBucket(rows, label) {
  const knownTokens = rows.filter(row => valid(row.tokens)), knownCosts = rows.filter(row => valid(row.cost));
  $('bucket-title').textContent = label || 'No activity';
  $('bucket-prompts').textContent = count(rows.length);
  $('bucket-prompt-unit').textContent = rows.length === 1 ? 'prompt' : 'prompts';
  $('bucket-tokens').textContent = knownTokens.length ? compact.format(total(knownTokens, 'tokens')) : '—';
  $('bucket-tokens').title = count(total(knownTokens, 'tokens')) + ' tokens';
  $('bucket-tokens').parentElement.hidden = !knownTokens.length;
  $('bucket-cost').textContent = costLabel(knownCosts.length ? total(knownCosts, 'cost') : null);
  $('bucket-cost').title = '$' + money(total(knownCosts, 'cost'));
  $('bucket-cost').parentElement.hidden = !knownCosts.length;
}

function renderActivity() {
  $('activity').hidden = !data?.rows?.length;
  if ($('activity').hidden) return;
  const rows = selected(), tokenRows = rows.filter(row => valid(row.tokens));
  const period = preferences.period, heatmap = period !== 'today';
  const unavailable = period === 'weekly' && !weeklyWindows().length;
  const key = heatmap && !unavailable ? renderHeatmaps(rows) : null;
  const detailRows = heatmap ? buckets.get(key)?.rows || [] : rows;
  if (promptId !== null && !detailRows.some(row => row.id === promptId)) promptId = null;
  const active = detailRows.find(row => row.id === promptId) || detailRows[0];
  $('detail-card').hidden = unavailable;
  $('heatmaps').hidden = !heatmap || unavailable;
  $('weekly-empty').hidden = !unavailable;
  // Today's totals are its summary line.
  $('totals').hidden = !heatmap;
  $('prompt-inspector').hidden = heatmap || !active;
  $('detail-body').hidden = !preferences.details;
  $('detail-toggle').setAttribute('aria-expanded', String(preferences.details));
  const signature = JSON.stringify([rows, promptId, agentFilter, period, key, document.documentElement.dataset.theme]);
  if (signature !== activitySignature) {
    activitySignature = signature;
    $('total-questions').textContent = unavailable ? '—' : count(rows.length);
    $('total-tokens').textContent = tokenRows.length ? compact.format(total(tokenRows, 'tokens')) : '—';
    $('total-tokens').title = count(total(tokenRows, 'tokens')) + ' tokens';
    if (!unavailable) renderBucket(detailRows, heatmap ? buckets.get(key)?.label : 'Today');
    if (!heatmap) {
      const picker = $('prompt');
      const options = JSON.stringify(rows.map(row => [row.id, row.title, row.start]));
      if (picker.dataset.options !== options) {
        picker.replaceChildren(new Option(rows.length ? 'Latest · ' + promptLabel(rows[0]) : 'No prompts', ''));
        for (const row of rows) picker.add(new Option(stamp(row.start) + ' · ' + promptLabel(row), row.id));
        picker.dataset.options = options;
      }
      picker.value = promptId || '';
      picker.title = active ? stamp(active.start) + ' · ' + agentName(active.agent) + ' · ' + promptLabel(active) : '';
      picker.disabled = !rows.length;
      renderPrompt(active);
    }
  }
  if (!unavailable) renderHistoryChart(detailRows, active);
}

function limitLabel(minutes) {
  return minutes === 10080 ? 'Weekly' : minutes === 1440 ? 'Daily'
    : minutes % 60 === 0 ? minutes / 60 + '-hour' : minutes + '-minute';
}

function countdown(item) {
  const remaining = Math.max(0, Math.ceil((item.resets_at - Date.now() / 1000) / 60));
  return remaining >= 1440 ? Math.floor(remaining / 1440) + 'd ' + Math.floor(remaining % 1440 / 60) + 'h'
    : remaining >= 60 ? Math.floor(remaining / 60) + 'h ' + remaining % 60 + 'm' : remaining + 'm';
}

function renderLimitOptions() {
  const durations = [...new Set(activeAccounts().flatMap(account => account.windows.map(w => w.minutes)))].sort((a, b) => b - a);
  // With the top bar showing limits, the widget's own limit display has nothing to configure.
  $('visible-limits').hidden = $('indicator-row').hidden = !durations.length || topBar;
  $('limit-setting').hidden = !durations.length;
  const options = $('limit-options'), signature = JSON.stringify(durations);
  if (options.dataset.signature !== signature) {
    options.dataset.signature = signature;
    options.replaceChildren(...durations.map(minutes => {
      const label = el('label'), input = el('input');
      input.type = 'checkbox'; input.value = minutes;
      label.append(input, el('span', limitLabel(minutes))); return label;
    }));
  }
  for (const input of options.querySelectorAll('input')) input.checked = !preferences.hiddenLimits.includes(Number(input.value));
}

function limitIndicator(account, item, buckets) {
  const node = el('div', undefined, 'limit'); node.dataset.minutes = item.minutes;
  const label = (buckets ? item.bucket + ' · ' : '') + limitLabel(item.minutes) + ' limit';
  const gauge = el('div', undefined, 'limit-gauge');
  gauge.setAttribute('role', 'meter'); gauge.setAttribute('aria-label', agentName(account.id) + ' · ' + label);
  gauge.setAttribute('aria-valuemin', '0'); gauge.setAttribute('aria-valuemax', '100');
  gauge.setAttribute('aria-valuenow', item.used_percent); gauge.setAttribute('aria-valuetext', percent.format(item.used_percent) + '% used');
  gauge.style.setProperty('--used', item.used_percent + '%');
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg'); svg.setAttribute('viewBox', '0 0 100 100'); svg.setAttribute('aria-hidden', 'true');
  for (const name of ['track', 'fill']) {
    const circle = document.createElementNS(svg.namespaceURI, 'circle');
    for (const [key, value] of Object.entries({ cx: 50, cy: 50, r: 44, pathLength: 100, class: name })) circle.setAttribute(key, value);
    if (name === 'fill') { circle.style.strokeDashoffset = 100 - item.used_percent; circle.style.visibility = item.used_percent === 0 ? 'hidden' : ''; }
    svg.append(circle);
  }
  const value = el('div', undefined, 'limit-value');
  value.append(el('strong', percent.format(item.used_percent) + '%', 'limit-percent'), el('span', 'used', 'limit-unit'));
  gauge.append(svg, value);
  const reset = el('time', 'Resets in ' + countdown(item), 'limit-reset'); reset.dateTime = new Date(item.resets_at * 1000).toISOString();
  reset.title = 'Resets ' + new Date(item.resets_at * 1000).toLocaleString('en-US', { timeZoneName: 'short' });
  const info = el('div', undefined, 'limit-info'); info.append(el('p', label, 'limit-label'), reset);
  if (preferences.limitAlert && account.status === 'available' && item.used_percent >= preferences.limitAlert) {
    node.dataset.warning = 'true';
    info.append(el('span', item.used_percent >= 100 ? 'Limit reached' : 'Near limit', 'limit-warning'));
  }
  node.append(gauge, info); return node;
}

function limitCard(account, windows) {
  const card = el('article', undefined, 'limit-card'); card.dataset.state = account.status; card.dataset.agent = account.id;
  card.style.setProperty('--accent', agentColor(account.id));
  const brand = el('p', undefined, 'limit-brand'); brand.append(agentIcon(account.id), el('span', agentName(account.id)));
  if (account.status === 'stale') brand.append(el('span', 'Outdated', 'limit-stale'));
  const list = el('div', undefined, 'limit-list');
  const buckets = new Set(windows.map(item => item.bucket)).size > 1;
  list.append(...windows.map(item => limitIndicator(account, item, buckets)));
  card.append(brand, list);
  card.title = 'Last reported ' + (valid(account.updated_at) ? stamp(account.updated_at) : 'unknown');
  return card;
}

const CONNECTION_MESSAGES = {
  disconnected: ['Disconnected', () => 'Monitoring is paused. Login and history are kept.'],
  not_found: ['Not installed', name => 'Install ' + name + ' in this environment.'],
  login_required: ['Not connected', () => 'Connect to sign in with your subscription.'],
  unsupported_account: ['Subscription required', () => 'API-key accounts do not report subscription limits.'],
  login_cancelled: ['Connection cancelled', () => 'No new connection was established.'],
  timeout: ['Connection timed out', () => 'Try connecting again.'],
  query_failed: ['Limits unavailable', () => 'The account query failed.'],
  unsupported_version: [name => 'Update ' + name, () => 'This version does not report usage limits.'],
};
function connectionMessage(account, id) {
  if (account?.windows?.length) return [account.status === 'stale' ? 'Last reading outdated' : 'Connected', ''];
  const [label, hint] = CONNECTION_MESSAGES[account?.issue] || ['Limits unavailable', () => 'No current limits were reported.'];
  const name = agentName(id);
  return [typeof label === 'function' ? label(name) : label, hint(name)];
}
let connectionTask = null, cancelling = false, connectionRevision = 0;
function renderConnections() {
  for (const id of agents.keys()) {
    const account = accounts.find(entry => entry.id === id);
    const [label, hint] = connectionMessage(account, id);
    const busy = connectionTask?.agent === id;
    $('connection-' + id).textContent = busy ? (connectionTask.method === 'disconnect' ? 'Disconnecting…' : 'Connecting…') : limitsLoaded ? label : 'Checking…';
    $('connection-' + id).title = hint;
    const button = document.querySelector('[data-connect="' + id + '"]');
    const canCancel = busy && connectionTask.method === 'connect';
    button.disabled = !!connectionTask && (!canCancel || cancelling);
    button.textContent = canCancel ? 'Cancel' : (account?.connected ?? !!account?.windows?.length) ? 'Disconnect' : 'Connect';
    button.title = account?.connected ? 'Pause monitoring; keep login and history' : 'Start monitoring';
  }
}

function buildAgentSettings() {
  const section = $('agent-settings');
  section.querySelectorAll('.agent-card').forEach(card => card.remove());
  for (const id of agents.keys()) {
    const card = el('article', undefined, 'agent-card');
    const header = el('div', undefined, 'agent-connection');
    const identity = el('div'), label = el('span', undefined, 'source-label');
    label.append(agentIcon(id), el('span', agentName(id)));
    const status = el('span', 'Checking…', 'caption'); status.id = 'connection-' + id; status.setAttribute('role', 'status');
    identity.append(label, status);
    const action = el('button', 'Connect', 'setting-action'); action.dataset.connect = id;
    header.append(identity, action);
    const source = el('details', undefined, 'agent-source'); source.id = 'source-details-' + id;
    const reset = el('button', 'Reset', 'text-button source-reset'); reset.dataset.default = id; reset.hidden = true;
    const folder = el('button', undefined, 'source-path'); folder.id = 'source-' + id; folder.dataset.source = id; folder.disabled = true;
    folder.setAttribute('aria-label', 'Choose ' + agentName(id) + ' history directory');
    folder.innerHTML = '<span>Loading…</span><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 7V5h6l2 2h10v13H3z"/></svg>';
    const empty = el('p', 'No session files found', 'caption source-empty'); empty.id = 'source-empty-' + id; empty.hidden = true;
    source.append(el('summary', 'History folder'), reset, folder, empty); card.append(header, source); section.append(card);
  }
}

$('agent-settings').addEventListener('click', async event => {
  const button = event.target.closest('button');
  if (!button) return;
  if (button.dataset.connect) {
    const agent = button.dataset.connect;
    settingsMessage();
    if (connectionTask) {
      if (connectionTask.agent !== agent || connectionTask.method !== 'connect') return;
      cancelling = true; renderConnections();
      try { await native('cancel_login', agent); } catch (error) { settingsMessage(error.message); }
      return;
    }
    const account = accounts.find(entry => entry.id === agent);
    const method = (account?.connected ?? !!account?.windows?.length) ? 'disconnect' : 'connect';
    connectionTask = { agent, method }; connectionRevision++; renderConnections();
    try {
      const result = await native(method, agent);
      if (result.agents) { accounts = result.agents; limitsLoaded = true; }
      else if (result.issue) settingsMessage(connectionMessage(result, agent)[1]);
    } catch (error) { settingsMessage(error.message); }
    finally { connectionTask = null; cancelling = false; render(); }
    return;
  }
  const name = button.dataset.source || button.dataset.default;
  if (!name) return;
  button.disabled = true; settingsMessage();
  try {
    const result = await native(button.dataset.source ? 'choose_directory' : 'reset_source', name);
    if (!result.cancelled) showSources(result);
  } catch (error) { settingsMessage(error.message); }
  finally { button.disabled = false; }
});

function renderLimits() {
  const visible = activeAccounts().filter(account => agentFilter === 'all' || account.id === agentFilter);
  const missing = limitsLoaded ? knownAgents().filter(id => (agentFilter === 'all' || id === agentFilter) && !visible.some(account => account.id === id))
    .map(id => accounts.find(account => account.id === id) || { id }) : [];
  const signature = JSON.stringify([visible, missing, preferences.limitAlert, preferences.hiddenLimits, document.documentElement.dataset.theme, Math.floor(Date.now() / 60000)]);
  renderLimitOptions();
  // The GNOME top bar already shows limits; the widget then keeps to activity.
  $('limits-section').hidden = topBar ||
    !visible.some(account => account.windows.some(w => !preferences.hiddenLimits.includes(w.minutes))) && !missing.length;
  if (signature === limitSignature) return;
  limitSignature = signature;
  $('limits').replaceChildren(...visible.flatMap(account => {
    const windows = account.windows.filter(item => !preferences.hiddenLimits.includes(item.minutes));
    return windows.length ? [limitCard(account, windows)] : [];
  }));
  for (const account of missing) {
    const [label, hint] = connectionMessage(account, account.id);
    const row = el('div', undefined, 'limit-unavailable');
    const action = el('button', label, 'text-button caption');
    action.addEventListener('click', () => openSettings());
    row.append(agentIcon(account.id), el('span', agentName(account.id)), action);
    row.title = hint;
    $('limits').append(row);
  }
}

function render() {
  updateAgents(); renderLimits(); renderConnections(); renderActivity();
  for (const id of agents.keys()) {
    $('source-empty-' + id).hidden = data?.source_counts?.[id] !== 0 || accounts.some(account => account.id === id && account.issue === 'disconnected');
  }
  const warnings = data?.warnings || [];
  $('connection-status').hidden = !failed && !warnings.length;
  $('connection').textContent = failed ? 'Not updating' : warnings.includes('Reading local history…') ? 'Reading history…' : 'Some usage missing';
  $('connection').dataset.state = failed ? 'error' : '';
  $('connection-info').textContent = failed ? 'Local history could not be refreshed.' : warnings.join(' ');
}

async function refresh(name) {
  const feed = feeds[name];
  if (feed.busy || document.hidden) return;
  feed.busy = true;
  const revision = connectionRevision;
  try {
    const next = await native(name);
    if (!Array.isArray(next[name === 'usage' ? 'rows' : 'agents'])) throw Error('Unavailable');
    feed.seconds = next.refresh_seconds || feed.seconds;
    if (name === 'usage') { data = next; failed = false; }
    else if (revision === connectionRevision && !connectionTask) { accounts = next.agents; topBar = next.top_bar === true; limitsLoaded = true; }
  } catch {
    if (name === 'usage') failed = true;
    else if (revision === connectionRevision && !connectionTask) { accounts = []; limitsLoaded = true; }
  } finally {
    feed.busy = false;
    clearTimeout(feed.timer);
    if (!document.hidden) { render(); feed.timer = setTimeout(() => refresh(name), feed.seconds * 1000); }
  }
}

function changePeriod(value) {
  preferences.period = value; updatePeriod();
  savePreferences(); promptId = null; bucketId = null; hideHeatmapTooltip(); render();
}

async function openSettings() {
  hideHeatmapTooltip(); updateWarningControl(); settingsMessage();
  for (const key of ['theme', 'style', 'indicator']) $(key + '-setting').value = preferences[key];
  $('pinned-setting').checked = preferences.pinned;
  $('settings').showModal(); reportSize();
  try { showSources(await native('sources')); } catch (error) { settingsMessage(error.message); }
}

function updateWarningControl() {
  $('limit-alert').value = preferences.limitAlert;
  const label = preferences.limitAlert ? preferences.limitAlert + '%' : 'Off';
  $('limit-value').textContent = label;
  $('limit-alert').setAttribute('aria-valuetext', label);
  $('limit-alert').style.setProperty('--fill', preferences.limitAlert + '%');
}

function showSources(sources) {
  for (const id of agents.keys()) {
    const button = $('source-' + id), source = sources[id];
    if (!button || !source) continue;
    button.disabled = false; button.querySelector('span').textContent = source.path;
    button.title = source.path + (source.exists ? '' : ' (not found)');
    document.querySelector('[data-default="' + id + '"]').hidden = !source.override;
  }
}

$('period').addEventListener('click', event => {
  const button = event.target.closest('[data-period]');
  if (button && button.dataset.period !== preferences.period) changePeriod(button.dataset.period);
});
$('detail-toggle').addEventListener('click', () => {
  preferences.details = !preferences.details; savePreferences(); renderActivity();
});
for (const key of ['style', 'indicator', 'theme']) $(key + '-setting').addEventListener('change', () => {
  preferences[key] = $(key + '-setting').value;
  applyTheme(); hideHeatmapTooltip(); render(); savePreferences(); reportSize();
});
$('limit-options').addEventListener('change', event => {
  const input = event.target;
  if (input.type !== 'checkbox') return;
  preferences.hiddenLimits = preferences.hiddenLimits.filter(value => value !== Number(input.value));
  if (!input.checked) preferences.hiddenLimits.push(Number(input.value));
  savePreferences(); renderLimits(); reportSize();
});
$('pinned-setting').addEventListener('change', () => { preferences.pinned = $('pinned-setting').checked; savePreferences(); });
$('help-toggle').addEventListener('click', () => {
  $('help').hidden = !$('help').hidden;
  $('help-toggle').setAttribute('aria-expanded', String(!$('help').hidden));
});
$('window-handle').addEventListener('pointerdown', event => {
  if (event.button !== 0 || event.target.closest('button, details, select, input, a')) return;
  event.preventDefault(); native('move').catch(error => settingsMessage(error.message));
});
$('widget-close').addEventListener('click', () => native('quit'));
$('prompt').addEventListener('change', () => { promptId = $('prompt').value || null; renderActivity(); });
$('heatmaps').addEventListener('mouseover', event => showHeatmapTooltip(event.target.closest('.heat-cell')));
$('heatmaps').addEventListener('focusin', event => showHeatmapTooltip(event.target.closest('.heat-cell')));
$('heatmaps').addEventListener('mouseleave', hideHeatmapTooltip);
$('heatmaps').addEventListener('focusout', hideHeatmapTooltip);
$('heatmaps').addEventListener('scroll', hideHeatmapTooltip, true);
const heatmapObserver = new ResizeObserver(entries => {
  for (const entry of entries) entry.target.closest('.heatmap').querySelector('.heatmap-labels').style.height = entry.contentRect.height + 'px';
});
let layoutRequest = '';
function reportSize() {
  const panel = $('settings').open ? $('settings') : document.querySelector('.widget');
  const height = Math.ceil(parseFloat(getComputedStyle(panel).height)) + 24;
  const request = JSON.stringify({ height });
  if (request === layoutRequest) return;
  layoutRequest = request;
  native('layout', { height }).catch(error => { layoutRequest = ''; settingsMessage(error.message); });
}
const sizeObserver = new ResizeObserver(reportSize);
document.querySelectorAll('.panel-content').forEach(content => sizeObserver.observe(content));
$('settings').addEventListener('close', reportSize);
window.addEventListener('resize', hideHeatmapTooltip);
window.addEventListener('scroll', hideHeatmapTooltip);
$('heatmaps').addEventListener('click', event => {
  const cell = event.target.closest('button');
  if (!cell || cell.disabled) return;
  bucketId = cell.dataset.key; promptId = null;
  hideHeatmapTooltip(); renderActivity();
});
$('heatmaps').addEventListener('keydown', event => {
  if (event.key === 'Escape') { hideHeatmapTooltip(); return; }
  const grid = event.target.closest('.heatmap-grid');
  if (!grid) return;
  const cells = [...grid.children], index = cells.indexOf(event.target);
  const strip = grid.closest('.heatmap').dataset.layout === 'days';
  const step = { ArrowLeft: strip ? -1 : -7, ArrowRight: strip ? 1 : 7, ArrowUp: strip ? 0 : -1, ArrowDown: strip ? 0 : 1 }[event.key];
  if (!step || index < 0) return;
  event.preventDefault();
  const next = cells[index + step];
  if (next && !next.disabled) { next.focus(); showHeatmapTooltip(next); }
});
$('settings-open').addEventListener('click', () => openSettings());
$('limit-alert').addEventListener('input', () => {
  preferences.limitAlert = Number($('limit-alert').value); updateWarningControl(); renderLimits();
});
$('limit-alert').addEventListener('change', savePreferences);
for (const method of ['export', 'import']) $(method + '-history').addEventListener('click', async () => {
  const button = $(method + '-history'); button.disabled = true; settingsMessage();
  try {
    const result = await native(method);
    if (!result.cancelled) {
      settingsMessage(method === 'export' ? 'Exported.' : result.imported ? count(result.imported) + ' records imported.' : 'Already up to date.');
      if (method === 'import') await refresh('usage');
    }
  } catch (error) { settingsMessage(error.message); }
  finally { button.disabled = false; }
});
systemTheme.addEventListener('change', () => { applyTheme(); render(); });
document.fonts.addEventListener('loadingdone', () => { chartSignature = ''; renderActivity(); });
document.addEventListener('visibilitychange', () => {
  for (const [name, feed] of Object.entries(feeds)) {
    clearTimeout(feed.timer);
    if (!document.hidden) refresh(name);
  }
});
Promise.all([native('agents'), native('preferences')]).then(([registry, saved]) => {
  for (const agent of registry) agents.set(agent.id, agent);
  buildAgentSettings();
  document.documentElement.dataset.demo = String(saved.demo === true);
  if (['system', 'dark', 'light'].includes(saved.theme)) preferences.theme = saved.theme;
  if (['today', 'weekly', 'all'].includes(saved.period)) preferences.period = saved.period;
  if (Number.isInteger(saved.limitAlert) && saved.limitAlert >= 0 && saved.limitAlert <= 100) preferences.limitAlert = saved.limitAlert;
  if (['glass', 'minimal', 'brutalist', 'neumorphic', 'swiss'].includes(saved.style)) preferences.style = saved.style;
  if (['bar', 'ring'].includes(saved.indicator)) preferences.indicator = saved.indicator;
  if (Array.isArray(saved.hiddenLimits)) preferences.hiddenLimits = [...new Set(saved.hiddenLimits.filter(value => Number.isInteger(value) && value > 0))];
  preferences.pinned = saved.pinned === true;
  preferences.details = saved.details === true;
  // Saving drops preferences that no longer exist.
  updatePeriod(); applyTheme(); savePreferences();
}).catch(error => settingsMessage(error.message)).finally(() => { refresh('usage'); refresh('limits'); });
