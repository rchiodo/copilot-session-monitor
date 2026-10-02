import http from 'node:http';
import path from 'node:path';
import os from 'node:os';
import { fileURLToPath } from 'node:url';
import { randomUUID, randomBytes } from 'node:crypto';
import { readFile, writeFile, mkdir, unlink, rename } from 'node:fs/promises';
import { spawn } from 'node:child_process';
import { createInterface } from 'node:readline';
import { LocalSource } from './source.mjs';
import { Ledger, SessionStore, unavailableSessions } from './engine.mjs';
import { FamilyMonitor } from './families.mjs';
import { MonitorActions, readDismissEntries } from './actions.mjs';

if (process.platform !== 'win32') throw new Error('This prototype requires Windows for native notifications and process evidence.');
if (Number(process.versions.node.split('.')[0]) < 24) throw new Error('Node.js 24 or newer is required.');
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const dataDir = path.join(root, '.local');
const port = Number(process.env.MONITOR_PORT ?? '43187');
if (!Number.isInteger(port) || port < 1024 || port > 65535) throw new Error('MONITOR_PORT must be 1024-65535.');
const url = `http://127.0.0.1:${port}`;
const instanceId = randomUUID();
const token = randomBytes(32).toString('hex');
const runtimeFile = path.join(dataDir, 'runtime.json');
const ledger = new Ledger(path.join(dataDir, 'notifications.json'));
await ledger.load();
const sessionStore = new SessionStore(path.join(dataDir, 'sessions.json'));
const retainedSessions = await sessionStore.load();
await mkdir(dataDir, { recursive: true });
let processes = null;
let bridge = null;
let stopping = false;
let timer;
let polling = false;
let bridgeReady = false;
let lastTest = 0;
let resetReason = null;
let theme = { mode: null, source: 'browser-fallback', reason: 'Waiting for Windows app preference' };
let themeCheckedAt = 0;
let notification = { state: 'starting', message: 'Starting Windows tray helper' };
let view = {
  instanceId, machine: os.hostname(), coverage: 'This Windows machine only',
  sessions: [], active: [], attention: [], issues: [], healthy: false,
  source: 'Read-only desktop metadata + local lifecycle events + live owner processes',
  updatedAt: null, notification,
};

const shorten = (value, length) => String(value).replace(/[\x00-\x1f]/g, ' ').slice(0, length);
async function notify(key, alert) {
  if (!await ledger.claim(key)) return;
  const id = randomUUID();
  const prefix = {
    finished: 'Family finished', waiting: 'Family needs attention', error: 'Family error',
    warning: 'Family status unavailable', test: 'TEST notification',
  }[alert.kind];
  notification = { id, state: 'queued', kind: alert.kind, at: new Date().toISOString(),
    title: `${prefix}: ${shorten(alert.title, 140)}`, message: shorten(alert.message, 220) };
  if (!bridgeReady || !bridge?.stdin.writable) {
    notification.state = 'failed';
    notification.message = 'Native notification helper unavailable. Alert was not delivered.';
    console.error(notification.message);
    return;
  }
  bridge.stdin.write(`${JSON.stringify({ type: 'notify', ...notification })}\n`);
}
const engine = new FamilyMonitor(os.hostname(), notify, retainedSessions, sessionStore.dismissed);
view = { ...view, ...engine.snapshot() };
const source = new LocalSource(path.join(os.homedir(), '.copilot'), () => processes);
for (const row of engine.rows.values()) source.tracked.add(row.id);

async function publish(result, status) {
  await sessionStore.save(result.members, engine.dismissed);
  view = { ...view, ...result, ...status, updatedAt: new Date().toISOString() };
}

async function poll() {
  if (polling || stopping) return;
  polling = true;
  try { await actions.run(pollOnce); } finally { polling = false; }
}

async function pollOnce() {
  try {
    if (resetReason) {
      const reason = resetReason;
      resetReason = null;
      const result = await engine.update([], { healthy: false, reason });
      await publish(result, { healthy: false, issues: [reason] });
      return;
    }
    const { samples, issues, desktopSessions, relatives } = await source.poll();
    if (resetReason) {
      const reason = resetReason;
      resetReason = null;
      const result = await engine.update([], { healthy: false, reason });
      await publish(result, { healthy: false, issues: [reason] });
      return;
    }
    const result = await engine.update(samples, { relatives });
    for (const id of engine.rows.keys()) source.tracked.add(id);
    source.releaseIdle(new Set(engine.rows.keys()));
    await publish(result, {
      issues: result.gap ? ['Sleep or polling gap detected; rebaselining without completion notifications.'] : issues,
      desktopSessions, healthy: !result.gap,
    });
  } catch (error) {
    const reason = `Local observer unavailable (${error.code ?? error.message})`;
    console.error(reason);
    try {
      const result = await engine.update([], { healthy: false, reason });
      await publish(result, { healthy: false, issues: [reason] });
    } catch (persistenceError) {
      view = { ...view, healthy: false, active: [],
        issues: [`Monitor persistence failed: ${persistenceError.code ?? persistenceError.message}`] };
    }
  }
}

const actions = new MonitorActions(engine, pollOnce, () => publish(engine.snapshot(), {}),
  () => !stopping && view.healthy && !resetReason && bridgeReady &&
    Date.now() - Date.parse(view.updatedAt) < 10000);

async function testNotification() {
  if (Date.now() - lastTest < 5000) return;
  lastTest = Date.now();
  await notify(`test:${randomUUID()}`, {
    kind: 'test', title: 'Local Copilot session monitor',
    message: `TEST ONLY - notifications work independently of Copilot and the browser. Source: ${os.hostname()}.`,
  });
}

async function stop() {
  if (stopping) return;
  stopping = true;
  clearInterval(timer);
  if (bridge?.stdin.writable) bridge.stdin.end(`${JSON.stringify({ type: 'stop' })}\n`);
  server.close();
  try {
    const runtime = JSON.parse(await readFile(runtimeFile, 'utf8'));
    if (runtime.instanceId === instanceId) await unlink(runtimeFile);
  } catch (error) {
    if (error.code !== 'ENOENT') console.error(`Could not remove monitor runtime file: ${error.message}`);
  }
  setTimeout(() => {
    if (bridge && bridge.exitCode === null) bridge.kill();
    process.exit(0);
  }, 1200);
}

const assets = new Map([
  ['/', ['index.html', 'text/html; charset=utf-8']],
  ['/app.js', ['app.js', 'text/javascript; charset=utf-8']],
  ['/style.css', ['style.css', 'text/css; charset=utf-8']],
]);
const server = http.createServer(async (req, res) => {
  const allowedHosts = new Set([`127.0.0.1:${port}`, `localhost:${port}`]);
  res.setHeader('Cache-Control', 'no-store');
  res.setHeader('X-Content-Type-Options', 'nosniff');
  res.setHeader('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'");
  const json = (status, data) => {
    res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8' });
    res.end(JSON.stringify(data));
  };
  if (!allowedHosts.has(req.headers.host) ||
      (req.headers.origin && ![`http://127.0.0.1:${port}`, `http://localhost:${port}`].includes(req.headers.origin)) ||
      req.headers['sec-fetch-site'] === 'cross-site') {
    json(403, { error: 'Local same-origin requests only' });
    return;
  }
  try {
    if (req.method === 'GET' && req.url === '/api/status') {
      const stale = view.updatedAt && Date.now() - Date.parse(view.updatedAt) > 10000;
      const themeStatus = bridgeReady && Date.now() - themeCheckedAt < 8000 ? theme
        : { mode: null, source: 'browser-fallback', reason: 'Windows theme reader unavailable' };
      const status = { ...view, notification, theme: themeStatus };
      if (stale || !status.healthy) {
        const reason = stale ? 'Observer is not producing fresh snapshots. Completion is unconfirmed.'
          : 'Observer unavailable; current run status is unconfirmed';
        status.sessions = unavailableSessions(status.sessions, reason);
        status.members = unavailableSessions(status.members ?? [], reason);
        status.active = [];
        status.attention = status.sessions.filter(row => ['waiting', 'error', 'unknown'].includes(row.state));
        status.healthy = false;
        if (stale) status.issues = [reason];
      }
      json(200, status);
    } else if (req.method === 'GET' && req.url === '/api/control') {
      json(200, { token });
    } else if (req.method === 'POST' && ['/api/test', '/api/stop', '/api/dismiss'].includes(req.url)) {
      if (req.headers.authorization !== `Bearer ${token}`) {
        json(403, { error: 'Monitor control token required' });
      } else if (req.url === '/api/test') {
        await testNotification();
        json(200, { notification });
      } else if (req.url === '/api/dismiss') {
        json(200, await actions.dismiss(await readDismissEntries(req)));
      } else {
        json(200, { stopping: true });
        await stop();
      }
    } else if (req.method === 'GET' && assets.has(req.url)) {
      const [file, type] = assets.get(req.url);
      res.writeHead(200, { 'Content-Type': type });
      res.end(await readFile(path.join(root, 'public', file)));
    } else {
      json(404, { error: 'Not found' });
    }
  } catch (error) {
    console.error(`HTTP handler failed: ${error.message}`);
    if (!res.headersSent) json(error.status ?? 500, { error: error.status ? error.message : 'Local monitor request failed' });
    else res.end();
  }
});
server.requestTimeout = 5000;
server.headersTimeout = 5000;
await new Promise((resolve, reject) => {
  server.once('error', reject);
  server.listen(port, '127.0.0.1', resolve);
});
await writeFile(`${runtimeFile}.tmp`, JSON.stringify({ pid: process.pid, instanceId, url, token }), 'utf8');
await rename(`${runtimeFile}.tmp`, runtimeFile);

const powershell = path.join(process.env.SystemRoot, 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe');
bridge = spawn(powershell, [
  '-NoLogo', '-NoProfile', '-NonInteractive', '-STA', '-ExecutionPolicy', 'Bypass',
  '-File', path.join(root, 'windows', 'tray.ps1'), '-MonitorUrl', url,
], { windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'] });
createInterface({ input: bridge.stdout }).on('line', async line => {
  try {
    const event = JSON.parse(line);
    if (event.type === 'ready') {
      bridgeReady = true;
      notification = { state: 'ready', message: 'Windows tray ready; use Test notification to check OS delivery.' };
    } else if (event.type === 'processes') {
      processes = { at: Date.now(), processes: event.processes };
      if (!timer) {
        timer = setInterval(poll, 1500);
        await poll();
      }
    } else if (event.type === 'power') {
      resetReason = `Windows power event: ${event.mode}; rebaselining`;
    } else if (event.type === 'theme') {
      themeCheckedAt = Date.now();
      theme = ['light', 'dark'].includes(event.mode)
        ? { mode: event.mode, source: 'windows-apps', observedAt: new Date(themeCheckedAt).toISOString() }
        : { mode: null, source: 'browser-fallback', reason: event.reason ?? 'Windows app preference unavailable' };
    } else if (event.type === 'notification-shown' || event.type === 'notification-submitted') {
      if (event.id === notification.id) {
        notification = { ...notification, state: event.type === 'notification-shown' || notification.state === 'shown' ? 'shown' : 'submitted' };
      }
    } else if (event.type === 'test') {
      await testNotification();
    } else if (event.type === 'stop') {
      await stop();
    } else if (event.type === 'error') {
      throw new Error(`Windows helper: ${event.message}`);
    }
  } catch (error) {
    processes = { error: true, at: Date.now() };
    console.error(`Bridge error: ${error.message}`);
    view = { ...view, healthy: false, active: [], issues: [`Windows helper error: ${error.message}`] };
  }
});
bridge.stderr.on('data', chunk => {
  console.error(`Windows helper stderr: ${chunk.toString().trim()}`);
});
bridge.stdin.on('error', error => {
  console.error(`Windows helper pipe failed: ${error.code}`);
});
bridge.on('error', error => {
  bridgeReady = false;
  notification = { state: 'failed', message: `Cannot start Windows helper: ${error.message}` };
  view = { ...view, healthy: false, active: [], issues: [notification.message] };
});
bridge.on('exit', code => {
  bridgeReady = false;
  processes = { error: true, at: Date.now() };
  if (!stopping) {
    notification = { state: 'failed', message: `Windows tray helper stopped (exit ${code}); restart the monitor.` };
    view = { ...view, healthy: false, active: [], issues: [notification.message] };
  }
});
process.on('SIGINT', stop);
process.on('SIGTERM', stop);
console.log(`Local Copilot session monitor: ${url} (PID ${process.pid})`);
