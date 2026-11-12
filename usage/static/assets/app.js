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
let data = null, accounts = [], scheduler = null, failed = false, limitsLoaded = false, topBar = false;
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

// Window scheduler: one switch, and the learned day as a chart whose start markers can be edited.
const SLOT_MINUTES = 30, LIKELY = 0.4, SNAP_MINUTES = 15;
const PAUSED = { disconnected: 'Agent disconnected', weekly_limit: 'Weekly limit reached', unavailable: 'Limits unavailable' };
const TRUST_LABELS = { weekly: 'weekly rhythm', routine: 'usual routine', recent: 'recent days' };
const scheduleEdits = new Map();
let scheduleRevision = 0;

function clockText(minutes) {
  const value = ((Math.round(minutes) % 1440) + 1440) % 1440;
  return String(Math.floor(value / 60)).padStart(2, '0') + ':' + String(value % 60).padStart(2, '0');
}

function clockMinutes(text) {
  const [hours, minutes] = String(text).split(':').map(Number);
  return hours * 60 + minutes;
}

function translucent(tone, alpha) {
  const hex = tone.replace('#', ''), full = hex.length === 3 ? [...hex].map(c => c + c).join('') : hex;
  if (!/^[0-9a-f]{6}$/i.test(full)) return tone;
  return 'rgba(' + [0, 2, 4].map(i => parseInt(full.slice(i, i + 2), 16)).join(', ') + ', ' + alpha + ')';
}

function schedulerStatus(entry) {
  const status = entry?.status || 'off';
  if (status === 'scheduled' && valid(entry.next_activation_at)) {
    const when = new Date(entry.next_activation_at * 1000);
    const days = Math.round((new Date(when).setHours(0, 0, 0, 0) - new Date().setHours(0, 0, 0, 0)) / 86400000);
    const time = clockText(when.getHours() * 60 + when.getMinutes());
    const label = days <= 0 ? 'Next ' + time : days === 1 ? 'Tomorrow ' + time : when.toLocaleDateString('en-US', { weekday: 'short' }) + ' ' + time;
    return [label, 'Next window start'];
  }
  return {
    paused: ['Paused', PAUSED[entry?.reason] || ''],
    learning: ['Learning', 'Needs a few days of history'],
    idle: ['Not needed', 'No start is expected to help in the coming days'],
  }[status] || ['Off', ''];
}

// Windows on the shown day: planned starts first, then natural starts where use is likely.
function schedulerWindows(starts, activity, span) {
  const events = [
    ...starts.map(minutes => [minutes, 0]),
    ...activity.flatMap((value, slot) => value >= LIKELY ? [[slot * SLOT_MINUTES, 1]] : []),
  ].sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  const windows = [];
  let end = -1;
  for (const [minutes, natural] of events) {
    if (minutes < end) continue;
    end = minutes + span; windows.push({ start: minutes, end, planned: !natural });
  }
  return windows;
}

function schedulerStarts(id) {
  return scheduleEdits.get(id)?.starts || (scheduler?.agents?.[id]?.plan?.starts || []).map(clockMinutes);
}

const schedulerOverlay = {
  id: 'schedulerOverlay',
  beforeDatasetsDraw(chart) {
    const overlay = chart.$scheduler; if (!overlay) return;
    const { ctx, chartArea: area, scales: { x } } = chart;
    ctx.save();
    overlay.windows.forEach((item, index) => {
      const left = x.getPixelForValue(item.start / 60), right = x.getPixelForValue(Math.min(item.end, 1440) / 60);
      ctx.fillStyle = translucent(overlay.band, index % 2 ? 0.05 : 0.1);
      ctx.fillRect(left, area.top, right - left, area.bottom - area.top);
    });
    ctx.restore();
  },
  afterDatasetsDraw(chart) {
    const overlay = chart.$scheduler; if (!overlay) return;
    const { ctx, chartArea: area, scales: { x } } = chart;
    ctx.save();
    // Resets: where a window ends inside the day and a fresh quota begins.
    ctx.strokeStyle = overlay.muted; ctx.lineWidth = 1; ctx.setLineDash([2, 2]);
    for (const item of overlay.windows) {
      if (item.end >= 1440) continue;
      const px = Math.round(x.getPixelForValue(item.end / 60)) + .5;
      ctx.beginPath(); ctx.moveTo(px, area.top); ctx.lineTo(px, area.bottom); ctx.stroke();
    }
    ctx.setLineDash([]);
    if (overlay.now !== null) {
      const px = x.getPixelForValue(overlay.now / 60);
      ctx.fillStyle = overlay.muted;
      ctx.beginPath(); ctx.moveTo(px, area.bottom - 5); ctx.lineTo(px - 3.5, area.bottom + 1); ctx.lineTo(px + 3.5, area.bottom + 1); ctx.fill();
    }
    ctx.font = '600 8px ' + Chart.defaults.font.family; ctx.textBaseline = 'middle';
    overlay.starts.forEach((minutes, index) => {
      const px = x.getPixelForValue(minutes / 60), top = area.top - 6;
      ctx.strokeStyle = overlay.marker; ctx.lineWidth = overlay.active === index ? 2 : 1.5;
      ctx.beginPath(); ctx.moveTo(px, top); ctx.lineTo(px, area.bottom); ctx.stroke();
      ctx.fillStyle = overlay.marker; ctx.strokeStyle = overlay.surface; ctx.lineWidth = 1.5;
      ctx.beginPath(); ctx.arc(px, top, overlay.active === index ? 5.5 : 4.5, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
      const label = clockText(minutes), width = ctx.measureText(label).width;
      const right = px + 8 + width <= chart.width;
      ctx.fillStyle = overlay.text; ctx.textAlign = right ? 'left' : 'right';
      ctx.fillText(label, right ? px + 8 : px - 8, top);
    });
    ctx.restore();
  },
};

function renderSchedulerChart(id) {
  const entry = scheduler?.agents?.[id], canvas = $('scheduler-chart-' + id), details = $('scheduler-details-' + id);
  if (!entry?.plan || !canvas || !$('settings').open || !details?.open || typeof Chart === 'undefined') return;
  const plan = entry.plan, span = entry.window_minutes || 300, edit = scheduleEdits.get(id);
  const activity = plan.activity?.length === 1440 / SLOT_MINUTES ? plan.activity : Array(1440 / SLOT_MINUTES).fill(0);
  const starts = schedulerStarts(id), enabled = entry.schedule?.enabled === true, tone = agentColor(id);
  const minute = new Date().getHours() * 60 + new Date().getMinutes();
  const overlay = {
    starts, windows: schedulerWindows(starts, activity, span), now: plan.today ? minute : null,
    active: edit?.index ?? -1, band: color('text'), marker: enabled ? tone : color('muted'),
    muted: color('muted'), text: color('text'), surface: color('panel') || color('surface'),
  };
  canvas.setAttribute('aria-label', agentName(id) + ' usage by hour' + (starts.length ? '; window starts at ' + starts.map(clockText).join(', ') : '; no window starts'));
  const key = 'scheduler-' + id;
  const signature = JSON.stringify([activity, overlay, document.documentElement.dataset.theme, preferences.style]);
  if (charts[key] && canvas.dataset.signature === signature) return;
  canvas.dataset.signature = signature;
  // One step per half hour: points sit at slot centers and steps change halfway between them,
  // so each value covers exactly its slot and the curve starts where use starts.
  const points = activity.map((value, slot) => ({ x: (slot + .5) * SLOT_MINUTES / 60, y: value }));
  const data = [{ x: 0, y: activity[0] }, ...points, { x: 24, y: activity[activity.length - 1] }];
  const datasets = [{
    data, borderColor: tone, backgroundColor: translucent(tone, .22), fill: 'origin', borderWidth: Number(color('chart-width')) * .75,
    stepped: 'middle', pointRadius: 0, pointHoverRadius: 3, pointHitRadius: 6, pointBackgroundColor: tone, clip: false,
  }];
  Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
  const options = {
    responsive: true, maintainAspectRatio: false, animation: false,
    layout: { padding: { left: 4, right: 8, top: 14 } },
    events: ['mousemove', 'mouseout'],
    interaction: { mode: 'nearest', intersect: false, axis: 'x' },
    plugins: {
      legend: { display: false },
      tooltip: {
        enabled: !edit, displayColors: false, backgroundColor: color('surface'), titleColor: color('muted'), bodyColor: color('text'),
        titleFont: { size: 9, weight: 'normal' }, bodyFont: { size: 11, weight: '600' },
        padding: 10, cornerRadius: Number(color('tooltip-radius')), caretSize: 3, caretPadding: 6, borderWidth: 0,
        filter: item => item.dataIndex > 0 && item.dataIndex <= activity.length,
        callbacks: {
          title: items => { const slot = items[0].dataIndex - 1; return clockText(slot * SLOT_MINUTES) + '–' + clockText((slot + 1) * SLOT_MINUTES); },
          label: item => Math.round(item.parsed.y * 100) + '% likely in use',
        },
      },
    },
    scales: {
      x: { type: 'linear', min: 0, max: 24, grid: { display: false }, border: { display: false },
        ticks: { stepSize: 6, maxRotation: 0, color: color('muted'), font: { size: 8 }, callback: value => clockText(value * 60) } },
      y: { min: 0, max: 1, display: false },
    },
  };
  if (charts[key]) {
    charts[key].$scheduler = overlay; charts[key].data = { datasets }; charts[key].options = options; charts[key].update('none');
  } else {
    charts[key] = new Chart(canvas, { type: 'line', data: { datasets }, options, plugins: [schedulerOverlay] });
    charts[key].$scheduler = overlay; charts[key].draw();
  }
}

function renderScheduler() {
  for (const id of agents.keys()) {
    const entry = scheduler?.agents?.[id], badge = $('scheduler-summary-' + id);
    if (!badge) continue;
    const [label, title] = schedulerStatus(entry);
    const failed = entry?.status === 'scheduled' && entry.last_event?.status === 'failed';
    badge.textContent = label; badge.title = failed ? 'The last start failed; it will be retried' : title;
    badge.dataset.state = failed ? 'failed' : entry?.status || 'off';
    $('scheduler-enabled-' + id).checked = entry?.schedule?.enabled === true;
    const custom = Array.isArray(entry?.schedule?.times);
    $('scheduler-reset-' + id).hidden = !custom;
    const source = $('scheduler-source-' + id), model = entry?.model;
    source.textContent = custom ? 'Your times' : model?.ready ? 'Learned · drag to adjust' : 'Learning your pattern';
    const lead = model?.trust && Object.entries(model.trust).sort((a, b) => b[1] - a[1])[0];
    source.title = !custom && model?.ready && lead
      ? count(model.active_days) + ' active days · currently follows your ' + TRUST_LABELS[lead[0]] + ' (' + Math.round(lead[1] * 100) + '%)'
      : '';
    renderSchedulerChart(id);
  }
}

async function saveSchedule(id, patch) {
  const current = scheduler?.agents?.[id]?.schedule || { enabled: false, times: null };
  scheduleRevision++; settingsMessage();
  try {
    const next = await native('save_schedule', { agent: id, schedule: { enabled: current.enabled === true, times: current.times ?? null, ...patch } });
    if (next?.agents) scheduler = next;
  } catch (error) { settingsMessage(error.message); }
  finally { scheduleRevision++; scheduleEdits.delete(id); renderScheduler(); reportSize(); }
}

// Press the chart to add a start, drag a marker to move it, click a marker to remove it.
function attachSchedulerChart(id, canvas) {
  const locate = event => {
    const chart = charts['scheduler-' + id], rect = canvas.getBoundingClientRect();
    const px = (event.clientX - rect.left) * (canvas.offsetWidth / rect.width || 1);
    return { chart, px, minutes: chart.scales.x.getValueForPixel(px) * 60 };
  };
  const snap = minutes => Math.min(1440 - SNAP_MINUTES, Math.max(0, Math.round(minutes / SNAP_MINUTES) * SNAP_MINUTES));
  canvas.addEventListener('pointerdown', event => {
    if (event.button !== 0 || !charts['scheduler-' + id] || scheduleEdits.has(id)) return;
    const { chart, px, minutes } = locate(event), span = scheduler?.agents?.[id]?.window_minutes || 300;
    if (px < chart.chartArea.left - 8 || px > chart.chartArea.right + 8) return;
    const starts = schedulerStarts(id).slice();
    let index = starts.findIndex(value => Math.abs(chart.scales.x.getPixelForValue(value / 60) - px) <= 8);
    const created = index < 0;
    if (created) {
      const value = snap(minutes);
      if (starts.some(other => Math.abs(other - value) < span)) return;
      starts.push(value); starts.sort((a, b) => a - b); index = starts.indexOf(value);
    }
    scheduleEdits.set(id, { starts, index, created, moved: false, origin: event.clientX });
    try { canvas.setPointerCapture(event.pointerId); } catch { /* synthetic pointers cannot be captured */ }
    event.preventDefault();
    renderSchedulerChart(id);
  });
  canvas.addEventListener('pointermove', event => {
    const edit = scheduleEdits.get(id);
    if (!edit || edit.index < 0) return;
    if (!edit.moved && Math.abs(event.clientX - edit.origin) < 4) return;
    edit.moved = true;
    const span = scheduler?.agents?.[id]?.window_minutes || 300;
    const low = edit.index > 0 ? edit.starts[edit.index - 1] + span : 0;
    const high = edit.index < edit.starts.length - 1 ? edit.starts[edit.index + 1] - span : 1440 - SNAP_MINUTES;
    if (low <= high) edit.starts[edit.index] = Math.min(high, Math.max(low, snap(locate(event).minutes)));
    renderSchedulerChart(id);
  });
  canvas.addEventListener('pointerup', event => {
    const edit = scheduleEdits.get(id);
    if (!edit || edit.index < 0) return;
    if (canvas.hasPointerCapture(event.pointerId)) canvas.releasePointerCapture(event.pointerId);
    const starts = !edit.moved && !edit.created ? edit.starts.filter((_, i) => i !== edit.index) : edit.starts;
    edit.starts = starts; edit.index = -1;
    // Without any start of your own, the scheduler goes back to learned times.
    saveSchedule(id, { times: starts.length ? starts.map(clockText) : null });
  });
  canvas.addEventListener('pointercancel', () => {
    if (scheduleEdits.get(id)?.index >= 0) { scheduleEdits.delete(id); renderSchedulerChart(id); }
  });
}

function buildAgentSettings() {
  const section = $('agent-settings');
  for (const key of Object.keys(charts)) {
    if (key.startsWith('scheduler-')) { charts[key].destroy(); delete charts[key]; }
  }
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
    source.append(el('summary', 'History folder'), reset, folder, empty);
    const sched = el('details', undefined, 'agent-scheduler'); sched.id = 'scheduler-details-' + id;
    sched.addEventListener('toggle', () => { renderScheduler(); reportSize(); });
    const schedSummary = el('summary'), schedBadge = el('span', 'Off', 'scheduler-summary'); schedBadge.id = 'scheduler-summary-' + id;
    schedSummary.append(el('span', 'Window scheduler'), schedBadge);
    const schedBody = el('div', undefined, 'scheduler-body');
    const toggleRow = el('label', undefined, 'scheduler-toggle'), toggle = el('input', undefined, 'switch');
    toggle.type = 'checkbox'; toggle.id = 'scheduler-enabled-' + id; toggle.dataset.schedulerEnabled = id; toggle.setAttribute('role', 'switch');
    toggleRow.append(el('span', 'Start windows automatically'), toggle);
    const chartWrap = el('div', undefined, 'scheduler-chart'), canvas = el('canvas'); canvas.id = 'scheduler-chart-' + id; canvas.setAttribute('role', 'img');
    chartWrap.append(canvas); attachSchedulerChart(id, canvas);
    const foot = el('p', undefined, 'scheduler-foot caption'), origin = el('span'); origin.id = 'scheduler-source-' + id;
    const learned = el('button', 'Use learned times', 'text-button'); learned.type = 'button'; learned.id = 'scheduler-reset-' + id; learned.dataset.schedulerReset = id; learned.hidden = true;
    foot.append(origin, learned);
    schedBody.append(toggleRow, chartWrap, foot);
    sched.append(schedSummary, schedBody);
    card.append(header, source, sched); section.append(card);
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
  if (name) {
    button.disabled = true; settingsMessage();
    try {
      const result = await native(button.dataset.source ? 'choose_directory' : 'reset_source', name);
      if (!result.cancelled) showSources(result);
    } catch (error) { settingsMessage(error.message); }
    finally { button.disabled = false; }
    return;
  }
  if (button.dataset.schedulerReset) await saveSchedule(button.dataset.schedulerReset, { times: null });
});

$('agent-settings').addEventListener('change', event => {
  const id = event.target.dataset.schedulerEnabled;
  if (id) saveSchedule(id, { enabled: event.target.checked });
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
  updateAgents(); renderLimits(); renderConnections(); renderScheduler(); renderActivity();
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
  const revision = connectionRevision, scheduled = scheduleRevision;
  try {
    const next = await native(name);
    if (!Array.isArray(next[name === 'usage' ? 'rows' : 'agents'])) throw Error('Unavailable');
    feed.seconds = next.refresh_seconds || feed.seconds;
    if (name === 'usage') { data = next; failed = false; }
    else if (revision === connectionRevision && !connectionTask) {
      accounts = next.agents; topBar = next.top_bar === true; limitsLoaded = true;
      if (next.scheduler && scheduled === scheduleRevision && !scheduleEdits.size) scheduler = next.scheduler;
    }
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
  try {
    const scheduled = scheduleRevision;
    const [sources, sched] = await Promise.all([native('sources'), native('scheduler')]);
    if (sched?.agents && scheduled === scheduleRevision && !scheduleEdits.size) scheduler = sched;
    showSources(sources); renderScheduler(); reportSize();
  } catch (error) { settingsMessage(error.message); }
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
for (const handle of document.querySelectorAll('#window-handle, .dialog-header')) {
  handle.addEventListener('pointerdown', event => {
    if (event.button !== 0 || event.target.closest('button, details, select, input, a')) return;
    event.preventDefault(); native('move').catch(error => settingsMessage(error.message));
  });
}
$('widget-close').addEventListener('click', () => native('hide'));
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
