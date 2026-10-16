// Exercise the shipped executable and Qt renderer without agent accounts or user data.
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { mkdtemp, rm, writeFile, mkdir } from 'node:fs/promises';
import { createServer } from 'node:net';
import { tmpdir } from 'node:os';
import { resolve, join } from 'node:path';

const executable = resolve(process.argv[2]);
const directory = await mkdtemp(join(tmpdir(), 'agent-usage-smoke-'));
const probe = createServer().listen(0, '127.0.0.1');
await once(probe, 'listening');
const port = probe.address().port;
await new Promise(done => probe.close(done));
const environment = { ...process.env, XDG_STATE_HOME: directory, LOCALAPPDATA: directory,
  XDG_CONFIG_HOME: directory, QT_QPA_PLATFORM: 'offscreen', QTWEBENGINE_REMOTE_DEBUGGING: `127.0.0.1:${port}` };
const child = spawn(executable, ['--demo'], { cwd: directory, env: environment, stdio: ['ignore', 'pipe', 'pipe'] });
let logs = '', socket;
for (const stream of [child.stdout, child.stderr]) stream.on('data', part => { logs = (logs + part).slice(-16000); });
const pause = ms => new Promise(done => setTimeout(done, ms));
const deadline = Date.now() + 60000;
async function until(read) {
  while (Date.now() < deadline) {
    const value = await read();
    if (value) return value;
    if (child.exitCode !== null) throw Error(logs);
    await pause(100);
  }
  throw Error('Widget did not become ready.\n' + logs);
}
try {
  const page = await until(async () => {
    try { return (await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()).find(p => p.type === 'page'); }
    catch { return null; }
  });
  socket = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise(done => socket.addEventListener('open', done, { once: true }));
  let sequence = 0;
  const pending = new Map();
  socket.addEventListener('message', event => {
    const message = JSON.parse(event.data);
    if (message.id && pending.has(message.id)) {
      const { done, timer } = pending.get(message.id);
      pending.delete(message.id); clearTimeout(timer); done(message);
    }
  });
  const send = (method, params = {}) => new Promise((done, reject) => {
    const id = ++sequence;
    const timer = setTimeout(() => { pending.delete(id); reject(Error(`Renderer timed out: ${method}`)); }, 10000);
    pending.set(id, { done, timer }); socket.send(JSON.stringify({ id, method, params }));
  });
  async function evaluate(expression) {
    const reply = await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
    assert(!reply.error && !reply.result.exceptionDetails, JSON.stringify(reply));
    return reply.result.result.value;
  }
  await until(() => evaluate("typeof data!=='undefined' && data?.rows.length>1500 && subscriptionsLoaded && document.documentElement.dataset.demo==='true'"));
  await evaluate("document.fonts.ready.then(()=>true)");
  assert.equal(await evaluate("providers.length"), 2);
  assert.equal(await evaluate("preferences.period"), 'all');
  const fits = `(()=>{const p=$('settings').open?$('settings'):document.querySelector('.widget'),r=p.getBoundingClientRect();return r.right<=innerWidth&&r.bottom<=innerHeight&&p.scrollHeight<=p.clientHeight+1})()`;
  for (const period of ['weekly', 'all']) {
    await evaluate(`changePeriod('${period}');true`); await pause(150);
    assert(await evaluate(fits));
    const size = await evaluate('[innerWidth,innerHeight]');
    await evaluate("[...$('heatmap-grid').children].find(c=>!c.disabled&&c.dataset.level==='0')?.click();true");
    await pause(100); assert.deepEqual(await evaluate('[innerWidth,innerHeight]'), size);
    await evaluate('openSettings().then(()=>true)'); await pause(100);
    assert(await evaluate(fits)); assert(await evaluate('innerHeight') < size[1]);
    await evaluate("$('settings').close();true"); await pause(100);
    assert.deepEqual(await evaluate('[innerWidth,innerHeight]'), size);
  }
  const historyHeight = await evaluate('innerHeight');
  await evaluate("changePeriod('today');true"); await pause(150);
  assert(await evaluate(fits)); assert(await evaluate('innerHeight') < historyHeight);
  for (const view of ['normal', 'compact']) {
    await evaluate(`$('view-setting').value='${view}';$('view-setting').dispatchEvent(new Event('change'));changePeriod('all');true`);
    await pause(200); assert(await evaluate(fits));
    if (process.env.PREVIEW_DIR) {
      await mkdir(process.env.PREVIEW_DIR, { recursive: true });
      const reply = await send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false });
      await writeFile(join(process.env.PREVIEW_DIR, view + '.png'), Buffer.from(reply.result.data, 'base64'));
    }
  }
  await evaluate("setTimeout(()=>native('quit'),0);true");
  await Promise.race([once(child, 'exit'), pause(5000)]);
  assert.equal(child.exitCode, 0, logs);
  console.log('Packaged widget: startup, demo, charts, Settings and shutdown passed.');
} finally {
  socket?.close();
  if (child.exitCode === null) { child.kill(); await once(child, 'exit'); }
  await rm(directory, { recursive: true, force: true });
}
