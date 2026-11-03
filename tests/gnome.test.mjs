// Presentation rules of the GNOME top bar indicator, without a running shell.
import assert from 'node:assert/strict';
import test from 'node:test';
import { accepts, countdown, limitLabel, summarize } from '../usage/gnome/limits.js';

const now = 1_800_000_000;
const limit = (agent, minutes, used, hours, bucket = null) =>
  ({ id: `${agent}:${bucket ?? ''}:${minutes}`, bucket, minutes, used_percent: used, resets_at: now + hours * 3600 });
const agent = (id, name, limits, extra = {}) => ({ id, name, color: '#123456', status: 'available', limits, ...extra });

test('only the app payload is accepted', () => {
  assert.equal(accepts({ agents: [], warning: 0, visible: true }), true);
  for (const body of [{ providers: [] }, { agents: {} }, null, [], 'agents'])
    assert.equal(accepts(body), false);
});

test('labels and countdowns match the widget', () => {
  assert.deepEqual([300, 1440, 10080, 90].map(limitLabel), ['5-hour', 'Daily', 'Weekly', '90-minute']);
  assert.deepEqual([30, 7200, 345600, -5].map(countdown), ['1m', '2h 0m', '4d 0h', '0m']);
});

test('the bar shows exactly the checked limits that are still reported', () => {
  const payload = { warning: 80, agents: [
    agent('beta', 'Beta', [limit('beta', 10080, 28, 48)]),
    agent('alpha', 'Alpha', [limit('alpha', 10080, 42, 96), limit('alpha', 300, 86.7, 1.2)]),
  ] };
  const unchecked = summarize(payload, now);
  assert.deepEqual(unchecked.agents.map(a => [a.name, a.limits.map(l => [l.label, l.value, l.level, l.reset])]), [
    ['Alpha', [['5-hour limit', '86%', 'warning', 'Resets in 1h 12m'], ['Weekly limit', '42%', 'normal', 'Resets in 4d 0h']]],
    ['Beta', [['Weekly limit', '28%', 'normal', 'Resets in 2d 0h']]],
  ]);
  assert.deepEqual(unchecked.shown, []);
  const checked = summarize(payload, now, ['beta::10080', 'alpha::10080', 'gone::300']);
  assert.deepEqual(checked.shown.map(l => l.id), ['alpha::10080', 'beta::10080']);
  assert.deepEqual(checked.agents[0].limits.map(l => l.selected), [false, true]);
  assert.deepEqual(summarize(payload, now, ['gone::300']).shown, []);
});

test('levels color the value and fill; outdated readings stay neutral', () => {
  const { agents } = summarize({ warning: 0, agents: [
    agent('alpha', 'Alpha', [limit('alpha', 300, 100, 1), limit('alpha', 10080, 99.9, 9)]),
    agent('beta', 'Beta', [limit('beta', 10080, 100, 9)], { status: 'stale', color: 'red; x: y' }),
  ] }, now);
  assert.deepEqual(agents[0].limits.map(l => [l.value, l.level, l.fill]), [['100%', 'critical', '#ff7b63'], ['99%', 'normal', '#123456']]);
  assert.deepEqual([agents[1].note, agents[1].limits[0].level, agents[1].limits[0].stale, agents[1].limits[0].fill],
    ['Outdated', 'normal', true, '#9a9a9a']);
});

test('expired, malformed and uninstalled entries are left out; issues explain empty agents', () => {
  const { agents, shown } = summarize({ agents: [
    agent('alpha', 'Alpha', [limit('alpha', 300, 5, -1)], { status: 'unavailable', issue: 'login_required' }),
    agent('beta', 'Beta', [], { status: 'unavailable', issue: 'not_found' }),
    agent('gamma', 'Gamma', [{ id: 'gamma::300', minutes: 300, used_percent: 'x' }], { issue: 'query_failed' }),
    agent('delta', 'Delta', [], { status: 'unavailable', issue: 'unsupported_version' }),
    agent('../escape', 'Escape', [limit('escape', 300, 5, 1)]),
    { id: 'nameless', limits: [limit('nameless', 300, 5, 1)] },
  ] }, now, ['alpha::300', 'escape::300', 'nameless::300']);
  assert.deepEqual(agents.map(a => [a.name, a.note, a.limits.length]),
    [['Alpha', 'Not connected', 0], ['Delta', 'Update Delta', 0], ['Gamma', 'Limits unavailable', 0]]);
  assert.deepEqual(shown, []);
  assert.deepEqual(summarize({}, now), { agents: [], shown: [] });
  assert.deepEqual(summarize(null, now), { agents: [], shown: [] });
});

test('several buckets of one agent are named', () => {
  const { agents } = summarize({ agents: [agent('alpha', 'Alpha', [
    limit('alpha', 10080, 10, 9, 'main'), limit('alpha', 10080, 20, 9, 'Large model'),
  ])] }, now);
  assert.deepEqual(agents[0].limits.map(l => l.label), ['main · Weekly limit', 'Large model · Weekly limit']);
});
