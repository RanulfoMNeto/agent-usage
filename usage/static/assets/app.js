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
const AGENTS = { Codex: { icon: 'chatgpt.svg', color: '--codex' }, 'Claude Code': { icon: 'claude.svg', color: '--claude' } };
let chartSignature = '';
let data = null, providers = [], failed = false, subscriptionsLoaded = false, quotaSignature = '';
let activitySignature = '';
let heatmapSignature = '', buckets = new Map(), bucketId = null;
const feeds = { usage: { seconds: 1 }, subscriptions: { seconds: 60 } };
let agentFilter = 'all', promptId = null;
const systemTheme = matchMedia('(prefers-color-scheme: dark)');
const preferences = { theme: 'dark', limitAlert: 0, period: 'today', view: 'normal', style: 'minimal', pinned: false, project: '' };
let sources = null;
const pending = new Map();
let sequence = 0;
const backendReady = new Promise(resolve => new QWebChannel(qt.webChannelTransport, channel => {
  const backend = channel.objects.backend;
  backend.layout_changed.connect((width, scale, compactView) => {
    const root = document.documentElement;
    root.style.setProperty('--layout-width', width + 'px');
    root.style.setProperty('--layout-scale', scale);
    root.dataset.narrow = String(width <= 360);
    root.dataset.layoutView = compactView ? 'compact' : 'normal';
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
  const label = document.documentElement.dataset.theme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme';
  $('theme-toggle').setAttribute('aria-label', label); $('theme-toggle').title = label;
  document.documentElement.dataset.style = preferences.style;
  document.documentElement.dataset.view = preferences.view;
}

function savePreferences() {
  native('save_preferences', preferences).catch(error => settingsMessage(error.message));
}

applyTheme();
$('period').value = preferences.period;

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

function activeProviders() {
  return providers.map(provider => ({ ...provider, windows: provider.windows.filter(item => item.resets_at > Date.now() / 1000) }))
    .filter(provider => provider.windows.length);
}

function knownAgents() {
  return [...new Set([...activeProviders().map(p => p.agent), ...(data?.rows || []).map(r => r.agent)])].filter(a => AGENTS[a]).sort();
}

function weeklyWindows() {
  return activeProviders().filter(p => agentFilter === 'all' || p.agent === agentFilter).flatMap(provider => {
    const item = provider.windows.find(w => w.minutes === 10080 && w.resets_at - w.minutes * 60 <= Date.now() / 1000);
    return item ? [{ agent: provider.agent, start: item.resets_at - item.minutes * 60, end: item.resets_at }] : [];
  });
}

function weeklyRange() {
  const windows = weeklyWindows();
  return windows.length ? { start: Math.min(...windows.map(w => w.start)), end: Math.max(...windows.map(w => w.end)) } : null;
}


function agentIcon(agent) {
  const img = el('img', undefined, 'agent-icon'); img.src = 'assets/' + AGENTS[agent].icon; img.alt = ''; img.width = 18; img.height = 18;
  img.dataset.agent = agent; return img;
}

function updateAgents() {
  const agents = knownAgents();
  if (agentFilter !== 'all' && !agents.includes(agentFilter)) { agentFilter = 'all'; promptId = null; bucketId = null; }
  const tabs = $('agents');
  if (tabs.dataset.options !== JSON.stringify(agents)) {
    tabs.replaceChildren();
    for (const agent of agents.length > 1 ? ['all', ...agents] : []) {
      const button = el('button'); button.type = 'button'; button.dataset.agent = agent;
      button.title = agent === 'all' ? 'Both agents' : agent;
      button.setAttribute('aria-label', button.title);
      if (agent !== 'all') button.append(agentIcon(agent));
      button.append(el('span', agent === 'all' ? 'Both' : agent.replace(' Code', '')));
      button.addEventListener('click', () => { agentFilter = agent; promptId = null; bucketId = null; render(); });
      tabs.append(button);
    }
    tabs.dataset.options = JSON.stringify(agents);
  }
  for (const button of tabs.children) button.setAttribute('aria-pressed', String(button.dataset.agent === agentFilter));
  tabs.hidden = agents.length < 2;
  $('dashboard-empty').hidden = agents.length > 0;
  $('dashboard-empty').textContent = (data || failed) && subscriptionsLoaded ? 'No agent usage data available.' : 'Loading usage…';
}

function agentRows() {
  return (data?.rows || []).filter(row => (agentFilter === 'all' || row.agent === agentFilter) && (
    !preferences.project || row.project === preferences.project ||
      row.project?.startsWith(preferences.project.replace(/\/$/, '') + '/')
  ));
}

function updateProjects() {
  const path = preferences.project;
  const label = path || 'All projects';
  $('project').querySelector('span').textContent = label;
  $('project').title = path ? label : 'Choose a repository folder to filter activity';
  $('project-reset').hidden = !path;
  $('project-scope').hidden = !path;
  $('project-scope').textContent = path.split('/').filter(Boolean).at(-1) || '/';
  $('project-scope').title = label + ' · Subscription limits apply to the whole account';
}

function selected() {
  const start = new Date(); start.setHours(0, 0, 0, 0);
  const period = $('period').value;
  const cutoff = period === 'all' ? 0 : start.getTime() / 1000;
  if (period === 'weekly') {
    const windows = weeklyWindows();
    return agentRows().filter(row => row.start <= Date.now() / 1000 && windows.some(w => w.agent === row.agent && row.start >= w.start && row.start < w.end));
  }
  return agentRows().filter(row => row.start >= cutoff && row.start <= Date.now() / 1000);
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

function bucketKey(start) {
  return dayKey(new Date(start * 1000));
}

function bucketSummary(bucket) {
  const prompts = count(bucket.prompts) + (bucket.prompts === 1 ? ' prompt' : ' prompts');
  return !bucket.prompts ? 'No recorded prompts' : prompts + (bucket.missing ? ' · ' + count(bucket.missing) + ' without token data' : '');
}

function hideHeatmapTooltip() {
  $('heatmap-tooltip').hidden = true;
  $('heatmap-grid').querySelector('[aria-describedby]')?.removeAttribute('aria-describedby');
}

function showHeatmapTooltip(cell) {
  const bucket = buckets.get(cell?.dataset.key);
  if (!bucket || cell.disabled) return;
  hideHeatmapTooltip();
  const popup = $('heatmap-tooltip');
  popup.replaceChildren(el('p', bucket.label, 'tooltip-date'));
  if (bucket.prompts) popup.append(el('strong', bucket.known ? compact.format(bucket.tokens) + ' tokens' : 'Tokens unavailable'));
  popup.append(el('p', bucketSummary(bucket), 'tooltip-summary'));
  popup.hidden = false;
  cell.setAttribute('aria-describedby', popup.id);
  const rect = cell.getBoundingClientRect(), box = popup.getBoundingClientRect();
  popup.style.left = Math.max(8, Math.min(innerWidth - box.width - 8, rect.x + rect.width / 2 - box.width / 2)) + 'px';
  popup.style.top = (rect.top >= box.height + 16 ? rect.top - box.height - 10 : rect.bottom + 10) + 'px';
}

function renderHeatmap(rows, key) {
  const period = $('period').value, weekly = period === 'weekly', now = new Date();
  const cycle = weekly ? weeklyRange() : null, strip = weekly;
  const today = new Date(now); today.setHours(0, 0, 0, 0);
  const signature = JSON.stringify([period, agentFilter, cycle, dayKey(today), rows.map(row => [row.id, row.start, row.tokens])]);
  const grid = $('heatmap-grid'), scroller = $('heatmap-scroll');
  if (signature !== heatmapSignature) {
    const previousMode = $('heatmap').dataset.period;
    const atEnd = scroller.scrollWidth - scroller.scrollLeft - scroller.clientWidth < 4;
    const focused = grid.contains(document.activeElement) ? document.activeElement.dataset.key : null;
    const hovered = grid.querySelector('[aria-describedby]')?.dataset.key;
    heatmapSignature = signature;
    $('heatmap').dataset.period = period;
    $('heatmap').dataset.layout = strip ? 'days' : 'weeks';
    $('heatmap').dataset.agent = agentFilter;
    buckets = new Map();
    for (const row of rows) {
      const id = bucketKey(row.start);
      if (!buckets.has(id)) buckets.set(id, { prompts: 0, known: 0, missing: 0, tokens: 0 });
      const bucket = buckets.get(id); bucket.prompts++;
      if (valid(row.tokens)) { bucket.tokens += row.tokens; bucket.known++; }
      else bucket.missing++;
    }
    const max = Array.from(buckets.values()).reduce((value, bucket) => Math.max(value, bucket.tokens), 1);
    const monday = shiftDay(today, -(today.getDay() + 6) % 7);
    let start = cycle ? new Date(cycle.start * 1000) : shiftDay(monday, -21 * 7);
    start.setHours(0, 0, 0, 0);
    if (!cycle && rows.length) {
      const first = new Date(rows.reduce((value, row) => Math.min(value, row.start), Infinity) * 1000); first.setHours(0, 0, 0, 0);
      if (first < start) start = shiftDay(first, -(first.getDay() + 6) % 7);
    }
    const end = cycle ? new Date((cycle.end - 1) * 1000) : shiftDay(monday, 6);
    const dates = [];
    for (let day = start; day <= end; day = shiftDay(day, 1)) dates.push(day);
    const columns = strip ? dates.length : dates.length / 7;
    $('heatmap').style.setProperty('--rows', strip ? 1 : 7);
    grid.style.setProperty('--columns', columns);
    $('heatmap-axis').style.setProperty('--columns', columns);
    $('heatmap-labels').replaceChildren(...(strip ? [] : ['M', '', 'W', '', 'F', '', 'S'].map(label => el('span', label))));
    const axis = [];
    if (strip) {
      for (const day of dates) axis.push(el('span', day.toLocaleDateString('en-US', { weekday: 'short' }) + '\n' + day.getDate()));
    } else {
      let lastColumn = -3;
      for (let index = 0; index < dates.length; index++) {
        const day = dates[index], column = Math.floor(index / 7);
        if (cycle && (day / 1000 < cycle.start || day / 1000 >= cycle.end)) continue;
        if ((!axis.length || day.getDate() === 1) && column - lastColumn >= 3) {
          const label = el('span', day.toLocaleDateString('en-US', { month: 'short' }));
          label.style.gridColumn = column + 1; axis.push(label); lastColumn = column;
        }
      }
    }
    $('heatmap-axis').replaceChildren(...axis);
    const cells = [];
    for (const day of dates) {
      const id = dayKey(day);
      const bucket = buckets.get(id) || { prompts: 0, known: 0, missing: 0, tokens: 0 };
      bucket.label = day.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
      buckets.set(id, bucket);
      const cell = el('button', undefined, 'heat-cell'); cell.type = 'button'; cell.tabIndex = -1; cell.dataset.key = id;
      cell.dataset.level = bucket.tokens ? Math.ceil(bucket.tokens / max * 4) : 0;
      cell.dataset.unknown = String(bucket.prompts > 0 && !bucket.known);
      cell.disabled = day > today;
      cell.setAttribute('aria-label', bucket.label + '. ' + (bucket.known ? count(bucket.tokens) + ' recorded tokens. ' : '') + bucketSummary(bucket));
      cells.push(cell);
    }
    hideHeatmapTooltip();
    grid.replaceChildren(...cells);
    const format = date => date.toLocaleDateString('en-US', { month: 'short', day: 'numeric', ...(weekly ? {} : { year: 'numeric' }) });
    $('heatmap-range').textContent = format(cycle ? new Date(cycle.start * 1000) : start) + ' – '
      + format(cycle ? new Date((cycle.end - (weekly ? 0 : 1)) * 1000) : today);
    $('heatmap-range').title = weekly ? weeklyWindows().map(w => w.agent + ': ' + stamp(w.start) + ' – ' + stamp(w.end)).join('\n') : '';
    if (!strip && (atEnd || previousMode !== period)) scroller.scrollLeft = scroller.scrollWidth;
    if (focused) cells.find(cell => cell.dataset.key === focused)?.focus({ preventScroll: true });
    if (hovered && !focused) showHeatmapTooltip(cells.find(cell => cell.dataset.key === hovered));
  }
  let selectedCell;
  for (const cell of grid.children) {
    const chosen = cell.dataset.key === key;
    cell.setAttribute('aria-pressed', String(chosen)); cell.tabIndex = chosen ? 0 : -1;
    if (chosen) selectedCell = cell;
  }
  if (!selectedCell) grid.querySelector('button:not(:disabled)')?.setAttribute('tabindex', '0');
}

function renderHistoryChart(rows, active) {
  const available = rows.some(row => valid(row.tokens));
  $('usage-chart').hidden = !available || typeof Chart === 'undefined';
  $('usage-empty').hidden = available && typeof Chart !== 'undefined';
  $('usage-empty').textContent = rows.length ? 'No token data' : 'No recorded prompts';
  const known = rows.filter(row => valid(row.tokens)).length;
  const missing = rows.length - known;
  $('line-coverage').style.visibility = missing && available ? '' : 'hidden';
  $('line-coverage').textContent = missing ? 'Gaps: ' + missing + (missing === 1 ? ' prompt' : ' prompts') + ' without token data.' : '';
  $('selection-key').hidden = !active;
  $('selection-key').dataset.missing = String(!valid(active?.tokens));
  $('selection-key').textContent = valid(active?.tokens) ? 'Selected' : 'Selected: no token data';
  if (!available || typeof Chart === 'undefined') { chartSignature = ''; return; }
  const signature = JSON.stringify([rows.map(r => [r.id, r.agent, r.start, r.tokens, r.title]), active?.id, document.documentElement.dataset.theme, preferences.style]);
  if (signature === chartSignature) return;
  chartSignature = signature;
  const agents = [...new Set(rows.map(row => row.agent))];
  const firstTime = Math.min(...rows.map(row => row.start)), lastTime = Math.max(...rows.map(row => row.start));
  const datasets = agents.map(agent => {
    const tone = color(AGENTS[agent].color.slice(2));
    return {
      label: agent, data: rows.filter(row => row.agent === agent).slice().reverse().map(row => ({ x: row.start, y: row.tokens, id: row.id, title: row.title })),
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
      legend: { display: agents.length > 1, labels: { usePointStyle: true, boxWidth: 5, boxHeight: 5, font: { size: 9 }, color: color('muted') } },
      tooltip: {
        displayColors: false, backgroundColor: color('surface'), titleColor: color('muted'), bodyColor: color('text'),
        titleFont: { size: 9, weight: 'normal' }, bodyFont: { size: 11, weight: '600' },
        padding: 12, cornerRadius: Number(color('tooltip-radius')), caretSize: 3, caretPadding: 7, borderWidth: 0,
        callbacks: {
          title: items => promptLabel(items[0].raw, 32),
          label: item => compact.format(item.parsed.y) + ' tokens',
          footer: items => stamp(items[0].parsed.x) + (agents.length > 1 ? ' · ' + items[0].dataset.label.replace(' Code', '') : ''),
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
  $('prompt-inspector').hidden = !row;
  $('prompt-detail').hidden = !row;
  $('cost-note').style.visibility = !row || !valid(row.cost) ? 'hidden' : '';
  if (!row) return;
  $('prompt-cost').textContent = costLabel(row.cost);
  $('prompt-cost').title = valid(row.cost) ? '$' + money(row.cost) : 'Cost unavailable';
  const costBasis = (row.cost_basis || 'API estimate') + '; not subscription billing' + (row.status === 'running' ? '; recorded so far' : '');
  $('cost-label').title = costBasis;
  $('cost-label').setAttribute('aria-label', costBasis);
  $('prompt-tokens').textContent = valid(row.tokens) ? compact.format(row.tokens) : '—';
  $('prompt-tokens').title = count(row.tokens) + ' tokens';
  $('prompt-duration').textContent = confirmed(row) ? duration(row.duration_ms).replaceAll(' min ', 'm ').replaceAll(' s', 's').replaceAll(' h ', 'h ') : row.status === 'running' ? 'Running' : '—';
  $('prompt-duration').title = confirmed(row) ? 'Confirmed duration' : 'Duration not confirmed';
}

function renderBucket(rows, key) {
  const bucket = buckets.get(key), knownTokens = rows.filter(row => valid(row.tokens)), knownCosts = rows.filter(row => valid(row.cost));
  $('bucket-title').textContent = bucket?.label || 'No recorded activity';
  $('bucket-detail').hidden = false;
  $('bucket-prompts').textContent = count(rows.length);
  $('bucket-tokens').textContent = knownTokens.length ? compact.format(total(knownTokens, 'tokens')) : '—';
  $('bucket-tokens').title = knownTokens.length ? count(total(knownTokens, 'tokens')) + ' recorded tokens' : 'Token data unavailable';
  $('bucket-cost').textContent = costLabel(knownCosts.length ? total(knownCosts, 'cost') : null);
  $('bucket-cost').title = knownCosts.length ? '$' + money(total(knownCosts, 'cost')) + ' API estimate; not subscription billing' : 'Cost estimate unavailable';
  for (const [id, known] of [['bucket-token-coverage', knownTokens], ['bucket-cost-coverage', knownCosts]]) {
    $(id).hidden = !rows.length || known.length === rows.length;
    $(id).textContent = count(known.length) + '/' + count(rows.length) + ' prompts';
    $(id).title = 'Prompts with recorded data for this metric';
  }
  $('bucket-cost-note').style.visibility = knownCosts.length ? '' : 'hidden';
}

function renderActivity() {
  $('activity').hidden = preferences.view === 'compact' || !data?.rows?.length;
  if ($('activity').hidden) return;
  const rows = selected(), tokenRows = rows.filter(row => valid(row.tokens));
  const period = $('period').value, heatmap = period !== 'today';
  const cycle = period === 'weekly' ? weeklyRange() : null;
  const unavailable = period === 'weekly' && !cycle;
  if (bucketId && cycle) {
    const from = new Date(bucketId + 'T00:00:00'), to = shiftDay(from, 1);
    if (to / 1000 <= cycle.start || from / 1000 >= cycle.end || from > new Date()) bucketId = null;
  }
  const key = heatmap ? bucketId || bucketKey(rows[0]?.start ?? Math.min(Date.now() / 1000, cycle?.end - 1 || Infinity)) : null;
  const detailRows = heatmap ? rows.filter(row => bucketKey(row.start) === key) : rows;
  if (promptId !== null && !detailRows.some(row => row.id === promptId)) promptId = null;
  const active = detailRows.find(row => row.id === promptId) || detailRows[0];
  $('detail-card').hidden = unavailable;
  $('heatmap').hidden = !heatmap || unavailable;
  $('bucket-inspector').hidden = !heatmap || unavailable;
  $('bucket-heading').hidden = !heatmap || unavailable;
  $('weekly-empty').hidden = !unavailable;
  $('prompt-inspector').hidden = heatmap || !active;
  if (heatmap && !unavailable) renderHeatmap(rows, key);
  const signature = JSON.stringify([rows, promptId, agentFilter, preferences.project, period, key, cycle]);
  if (signature !== activitySignature) {
    activitySignature = signature;
    $('total-questions').textContent = unavailable ? '—' : count(rows.length);
    $('total-tokens').textContent = tokenRows.length ? compact.format(total(tokenRows, 'tokens')) : '—';
    $('total-tokens').title = count(total(tokenRows, 'tokens')) + ' recorded tokens';
    if (heatmap) {
      if (!unavailable) renderBucket(detailRows, key);
    }
    else {
      const picker = $('prompt');
      const options = JSON.stringify(rows.map(row => [row.id, row.title, row.start]));
      if (picker.dataset.options !== options) {
        picker.replaceChildren(new Option(rows.length ? 'Latest · ' + promptLabel(rows[0]) : 'No prompts', ''));
        for (const row of rows) picker.add(new Option(stamp(row.start) + ' · ' + promptLabel(row), row.id));
        picker.dataset.options = options;
      }
      picker.value = promptId || '';
      picker.title = active ? stamp(active.start) + ' · ' + active.agent + ' · ' + promptLabel(active) : '';
      picker.disabled = !rows.length;
      renderPrompt(active);
    }
  }
  if (!unavailable) renderHistoryChart(detailRows, active);
  $('chart-coverage').hidden = true;
  if (period === 'weekly') {
    const windows = weeklyWindows();
    const missing = knownAgents().filter(agent => (agentFilter === 'all' || agent === agentFilter) && !windows.some(w => w.agent === agent));
    const notes = [];
    if (missing.length && !unavailable) notes.push(windows.map(w => w.agent.replace(' Code', '')).join(' + ') + ' cycle only.');
    if (new Set(windows.map(w => w.start)).size > 1) notes.push("Each agent uses its own cycle.");
    $('chart-coverage').hidden = !notes.length;
    $('chart-coverage').textContent = notes.join(' ');
  }
}

function quotaCard(provider, item, multipleBuckets) {
  const card = el('article', undefined, 'quota-card'); card.dataset.state = provider.status; card.dataset.agent = provider.agent;
  const minutes = item.minutes;
  const period = minutes === 10080 ? 'Weekly limit' : minutes === 1440 ? 'Daily limit'
    : minutes % 60 === 0 ? minutes / 60 + '-hour limit' : minutes + '-minute limit';
  const label = (multipleBuckets ? item.name + ' · ' : '') + period;
  const remaining = Math.max(0, Math.ceil((item.resets_at - Date.now() / 1000) / 60));
  const countdown = remaining >= 1440 ? Math.floor(remaining / 1440) + 'd ' + Math.floor(remaining % 1440 / 60) + 'h'
    : remaining >= 60 ? Math.floor(remaining / 60) + 'h ' + remaining % 60 + 'm' : remaining + 'm';
  const gauge = el('div', undefined, 'quota-gauge');
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg'); svg.setAttribute('viewBox', '0 0 100 100'); svg.setAttribute('aria-hidden', 'true');
  for (const name of ['track', 'fill']) {
    const circle = document.createElementNS(svg.namespaceURI, 'circle');
    for (const [key, value] of Object.entries({ cx: 50, cy: 50, r: 44, pathLength: 100, class: name })) circle.setAttribute(key, value);
    if (name === 'fill') { circle.style.strokeDashoffset = 100 - item.used_percent; circle.style.visibility = item.used_percent === 0 ? 'hidden' : ''; }
    svg.append(circle);
  }
  const value = el('div', undefined, 'quota-value');
  value.append(el('strong', percent.format(item.used_percent) + '%', 'quota-percent'), el('span', 'used', 'quota-unit'));
  gauge.append(svg, value);
  const brand = el('p', undefined, 'quota-brand'); brand.append(agentIcon(provider.agent), el('span', provider.agent.replace(' Code', '')));
  const reset = el('time', 'Resets in ' + countdown, 'quota-reset'); reset.dateTime = new Date(item.resets_at * 1000).toISOString();
  reset.title = 'Resets ' + new Date(item.resets_at * 1000).toLocaleString('en-US', { timeZoneName: 'short' });
  const info = el('div', undefined, 'quota-info'); info.append(brand, el('p', label, 'quota-label'), reset);
  if (provider.status === 'stale') info.append(el('span', 'Outdated', 'quota-stale'));
  if (preferences.limitAlert && provider.status === 'available' && item.used_percent >= preferences.limitAlert) {
    card.dataset.warning = 'true';
    info.append(el('span', item.used_percent >= 100 ? 'Limit reached' : 'Near limit', 'quota-warning'));
  }
  card.append(gauge, info);
  card.title = 'Last reported ' + (valid(provider.updated_at) ? stamp(provider.updated_at) : 'unknown');
  return card;
}

function renderSubscriptions() {
  const visible = activeProviders().filter(p => agentFilter === 'all' || p.agent === agentFilter);
  const missing = subscriptionsLoaded ? knownAgents().filter(agent => (agentFilter === 'all' || agent === agentFilter) && !visible.some(p => p.agent === agent))
    .map(agent => providers.find(p => p.agent === agent) || { agent }) : [];
  const signature = JSON.stringify([visible, missing, preferences.limitAlert, Math.floor(Date.now() / 60000)]);
  const hasLimits = activeProviders().length > 0;
  $('limit-setting').hidden = !hasLimits;
  $('subscriptions-section').hidden = !visible.length && !missing.length;
  if (signature === quotaSignature) return;
  quotaSignature = signature;
  $('subscriptions').replaceChildren(...visible.flatMap(provider => {
    const multipleBuckets = new Set(provider.windows.map(item => item.name)).size > 1;
    return provider.windows.map(item => quotaCard(provider, item, multipleBuckets));
  }));
  for (const provider of missing) {
    const { agent } = provider;
    const [label, hint] = {
      not_found: ['Codex not found', 'Install the Codex CLI or its official VS Code extension.'],
      login_required: ['Sign in required', 'Sign in to Codex with ChatGPT in this Windows or Linux environment.'],
      unsupported_account: ['ChatGPT login required', 'This account does not provide ChatGPT subscription limits.'],
      timeout: ['Connection timed out', 'The account query timed out. Retrying automatically.'],
      query_failed: ['Limits unavailable', 'The native account query failed. Retrying automatically.'],
      claude_waiting: ['Waiting for Claude CLI', 'Complete a request in Claude Code terminal mode with your subscription. The VS Code chat panel does not supply this status-line snapshot.'],
      claude_no_limits: ['No limits reported', 'The status line is connected, but Claude has not supplied subscription limits. Complete a request using an eligible subscription account.'],
      claude_not_found: ['Claude not detected', 'Start Claude Code in this environment, then reopen the widget to connect its status line.'],
      claude_custom_statusline: ['Custom status line', 'Your existing Claude status line was preserved. See the README to connect its output.'],
      claude_setup_failed: ['Setup unavailable', 'Claude settings could not be updated. Check file permissions and valid JSON, then reopen the widget.'],
      claude_setup_disabled: ['Setup disabled', 'Automatic Claude status-line setup is disabled in the widget configuration.'],
    }[provider.issue] || ['Limits unavailable', 'No current account limits were reported.'];
    const row = el('div', undefined, 'quota-unavailable');
    row.append(agentIcon(agent), el('span', agent.replace(' Code', '')), el('span', label, 'caption'));
    row.title = hint;
    $('subscriptions').append(row);
  }
  $('subscriptions').dataset.cards = $('subscriptions').querySelectorAll('.quota-card').length;
}

function render() {
  updateAgents(); updateProjects(); renderSubscriptions(); renderActivity();
  for (const name of ['codex', 'claude']) {
    $('source-empty-' + name).hidden = data?.source_counts?.[name] !== 0;
  }
  const warnings = preferences.view === 'compact' ? [] : data?.warnings || [];
  $('connection-status').hidden = preferences.view === 'compact' || (!failed && !warnings.length);
  $('connection').textContent = failed ? 'Not updating' : warnings.includes('Reading local history…') ? 'Reading history…' : 'Some usage missing';
  $('connection').dataset.state = failed ? 'error' : '';
  $('connection-info').textContent = failed ? 'Local history could not be refreshed.' : warnings.join(' ');
}

async function refresh(name) {
  const feed = feeds[name];
  if (feed.busy || document.hidden) return;
  feed.busy = true;
  try {
    const next = await native(name);
    if (!Array.isArray(next[name === 'usage' ? 'rows' : 'providers'])) throw Error('Unavailable');
    feed.seconds = next.refresh_seconds || feed.seconds;
    if (name === 'usage') { data = next; failed = false; }
    else { providers = next.providers; subscriptionsLoaded = true; }
  } catch {
    if (name === 'usage') failed = true;
    else { providers = []; subscriptionsLoaded = true; }
  } finally {
    feed.busy = false;
    clearTimeout(feed.timer);
    if (!document.hidden) { render(); feed.timer = setTimeout(() => refresh(name), feed.seconds * 1000); }
  }
}

function changePeriod(value) {
  preferences.period = value; $('period').value = value;
  savePreferences(); promptId = null; bucketId = null; hideHeatmapTooltip(); render();
}

async function openSettings() {
  hideHeatmapTooltip(); updateWarningControl(); settingsMessage();
  $('view-setting').value = preferences.view; $('style-setting').value = preferences.style; $('pinned-setting').checked = preferences.pinned;
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

function showSources(next) {
  sources = next;
  for (const name of ['codex', 'claude']) {
    const button = $('source-' + name);
    button.disabled = false; button.querySelector('span').textContent = sources[name].path;
    button.title = sources[name].path + (sources[name].exists ? '' : ' (not found)');
    document.querySelector('[data-default="' + name + '"]').hidden = !sources[name].override;
  }
}
$('source-settings').addEventListener('click', async event => {
  const button = event.target.closest('button');
  if (!button) return;
  const name = button.dataset.source || button.dataset.default;
  if (!name) return;
  button.disabled = true; settingsMessage();
  try {
    const result = await native(button.dataset.source ? 'choose_directory' : 'reset_source', name);
    if (!result.cancelled) showSources(result);
  } catch (error) { settingsMessage(error.message); }
  finally { button.disabled = false; }
});
$('period').addEventListener('change', () => changePeriod($('period').value));
async function chooseProject(reset = false) {
  $('project').disabled = true; settingsMessage();
  try {
    const result = reset ? {path: ''} : await native('choose_directory', 'project');
    if (result.cancelled) return;
    preferences.project = result.path || ''; promptId = null; bucketId = null;
    savePreferences(); hideHeatmapTooltip(); render();
  } catch (error) { settingsMessage(error.message); }
  finally { $('project').disabled = false; }
}
$('project').addEventListener('click', () => chooseProject());
$('project-reset').addEventListener('click', () => chooseProject(true));
for (const key of ['view', 'style']) $(key + '-setting').addEventListener('change', () => {
  preferences[key] = $(key + '-setting').value;
  applyTheme(); render(); savePreferences(); reportSize();
});
$('pinned-setting').addEventListener('change', () => { preferences.pinned = $('pinned-setting').checked; savePreferences(); });
document.querySelector('.widget').addEventListener('pointerdown', event => {
  if (event.button !== 0 || event.target.closest('button, details, select, input, a')) return;
  if (preferences.view !== 'compact' && !event.target.closest('.header')) return;
  event.preventDefault(); native('move').catch(error => settingsMessage(error.message));
});
$('widget-close').addEventListener('click', () => native('quit'));
$('prompt').addEventListener('change', () => { promptId = $('prompt').value || null; renderActivity(); });
$('heatmap-grid').addEventListener('mouseover', event => showHeatmapTooltip(event.target.closest('button')));
$('heatmap-grid').addEventListener('focusin', event => showHeatmapTooltip(event.target.closest('button')));
$('heatmap-grid').addEventListener('mouseleave', hideHeatmapTooltip);
$('heatmap-grid').addEventListener('focusout', hideHeatmapTooltip);
$('heatmap-scroll').addEventListener('scroll', hideHeatmapTooltip);
new ResizeObserver(([entry]) => {
  $('heatmap-labels').style.height = entry.contentRect.height + 'px';
}).observe($('heatmap-grid'));
let layoutRequest = '';
function reportSize() {
  if (document.documentElement.dataset.layoutView !== preferences.view) return;
  const panel = $('settings').open ? $('settings') : document.querySelector('.widget');
  const height = Math.ceil(parseFloat(getComputedStyle(panel).height)) + 24;
  const args = { height, compact: preferences.view === 'compact' };
  const request = JSON.stringify(args);
  if (request === layoutRequest) return;
  layoutRequest = request;
  native('layout', args).catch(error => { layoutRequest = ''; settingsMessage(error.message); });
}
const sizeObserver = new ResizeObserver(reportSize);
document.querySelectorAll('.panel-content').forEach(content => sizeObserver.observe(content));
$('settings').addEventListener('close', reportSize);
window.addEventListener('resize', hideHeatmapTooltip);
window.addEventListener('scroll', hideHeatmapTooltip);
$('heatmap-grid').addEventListener('click', event => {
  const cell = event.target.closest('button');
  if (!cell || cell.disabled) return;
  bucketId = cell.dataset.key; promptId = null;
  hideHeatmapTooltip(); renderActivity();
});
$('heatmap-grid').addEventListener('keydown', event => {
  if (event.key === 'Escape') { hideHeatmapTooltip(); return; }
  const cells = [...$('heatmap-grid').children], index = cells.indexOf(event.target);
  const strip = $('heatmap').dataset.layout === 'days';
  const step = { ArrowLeft: strip ? -1 : -7, ArrowRight: strip ? 1 : 7, ArrowUp: strip ? 0 : -1, ArrowDown: strip ? 0 : 1 }[event.key];
  if (!step || index < 0) return;
  event.preventDefault();
  const next = cells[index + step];
  if (next && !next.disabled) { next.focus(); showHeatmapTooltip(next); }
});
$('settings-open').addEventListener('click', () => openSettings());
$('theme-toggle').addEventListener('click', () => {
  preferences.theme = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  savePreferences(); applyTheme(); hideHeatmapTooltip(); render();
});
$('limit-alert').addEventListener('input', () => {
  preferences.limitAlert = Number($('limit-alert').value); updateWarningControl(); renderSubscriptions();
});
$('limit-alert').addEventListener('change', savePreferences);
for (const method of ['export', 'import']) $(''+method+'-history').addEventListener('click', async () => {
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
native('preferences').then(saved => {
  document.documentElement.dataset.demo = String(saved.demo === true);
  if (['system', 'dark', 'light'].includes(saved.theme)) preferences.theme = saved.theme;
  if (['today', 'weekly', 'all'].includes(saved.period)) preferences.period = saved.period;
  if (Number.isInteger(saved.limitAlert) && saved.limitAlert >= 0 && saved.limitAlert <= 100) preferences.limitAlert = saved.limitAlert;
  if (['normal', 'compact'].includes(saved.view)) preferences.view = saved.view;
  if (['glass', 'minimal', 'brutalist', 'neumorphic', 'swiss'].includes(saved.style)) preferences.style = saved.style;
  preferences.pinned = saved.pinned === true;
  if (typeof saved.project === 'string' && saved.project !== '?' && saved.project.length <= 4096) preferences.project = saved.project;
  $('period').value = preferences.period; applyTheme(); savePreferences();
}).catch(error => settingsMessage(error.message)).finally(() => { refresh('usage'); refresh('subscriptions'); });
