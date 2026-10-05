import http from 'node:http';
import https from 'node:https';
import path from 'node:path';
import os from 'node:os';
import { randomUUID, randomBytes } from 'node:crypto';
import { readFile, mkdir, unlink } from 'node:fs/promises';
import { spawn } from 'node:child_process';
import { createInterface } from 'node:readline';
import { Ledger, SessionStore } from './engine.mjs';
import { Collector } from './collector.mjs';
import { MonitorActions, readDismissEntries } from './actions.mjs';
import { root, dataDir, powershell, loadCollector, saveConfig, optionalConfig, migrateLegacy, pairConnectionString, ensureLocalReporter } from './configuration.mjs';
import { acquireRole } from './lifecycle.mjs';
import { authorized, readJson, digest, fail, HASH } from './protocol.mjs';
import { createLocalObserver, pollLocal, localReportPayload } from './local-report.mjs';

if (process.platform !== 'win32') throw new Error('The collector requires Windows for native notifications.');
if (Number(process.versions.node.split('.')[0]) < 24) throw new Error('Node.js 24 or newer is required.');
const port = Number(process.env.MONITOR_PORT ?? '43187');
if (!Number.isInteger(port) || port < 1024 || port > 65535) throw new Error('MONITOR_PORT must be 1024-65535.');
const url = `http://127.0.0.1:${port}`, instanceId = randomUUID(), token = randomBytes(32).toString('hex');
// A dedicated hub (pure aggregation display, no coding on it) can opt out of
// watching its own machine; everything else about self-observation below
// depends on this flag so a disabled hub never auto-creates a legacy
// reporter identity it will never use.
const selfObserve = (process.env.MONITOR_SELF_OBSERVE ?? '1') !== '0';
await mkdir(dataDir, { recursive: true });
const release = await acquireRole(dataDir, 'collector');
const config = await loadCollector();
const localReporterId = selfObserve ? await ensureLocalReporter(config) : null;
const collectorFile = path.join(dataDir, 'collector-state.json');
await migrateLegacy(config, collectorFile);
const legacy = new SessionStore(path.join(dataDir, 'sessions.json'));
const retained = await legacy.load();
const ledger = new Ledger(path.join(dataDir, 'notifications.json'));
await ledger.load();
let bridge, bridgeReady = false, stopping = false, lastTest = 0, themeCheckedAt = 0;
let fault = null;
let theme = { mode: null, source: 'browser-fallback', reason: 'Waiting for Windows app preference' };
let notification = { state: 'starting', message: 'Starting Windows tray helper' };

async function notify(key, alert) {
  if (!await ledger.claimDigest(key)) return;
  const prefix = { finished: 'Family finished', waiting: 'Family needs attention', error: 'Family error',
    warning: 'Family status unavailable', test: 'TEST notification' }[alert.kind];
  const shorten = (value, length) => String(value).replace(/[\x00-\x1f]/g, ' ').slice(0, length);
  notification = { id: randomUUID(), state: 'queued', kind: alert.kind, at: new Date().toISOString(),
    title: `${prefix}: ${shorten(alert.title, 140)}`, message: shorten(alert.message, 220) };
  if (!bridgeReady || !bridge?.stdin.writable) {
    notification.state = 'failed';
    notification.message = 'Native notification helper unavailable. Alert was not delivered.';
    console.error(notification.message);
  } else bridge.stdin.write(`${JSON.stringify({ type: 'notify', ...notification })}\n`);
}
const collector = new Collector(collectorFile, notify);
await collector.load(config, retained, legacy.dismissed);
async function refreshConfiguration() {
  const next = await loadCollector();
  if (next.id !== config.id || next.bindAddress !== config.bindAddress || next.port !== config.port) {
    throw new Error('Collector listener configuration changed; restart required');
  }
  collector.configure(next);
}
const actions = new MonitorActions(collector, refreshConfiguration, () => collector.save(),
  () => !stopping && !fault && bridgeReady);

// Self-observation: the collector watches its OWN machine's Copilot sessions
// in-process, using the same always-present "legacy" reporter identity a
// local watcher would otherwise use, so a separate paired watcher is never
// required just to see the host's own activity. A pre-existing split install
// (an already-running separate watcher.mjs for this same machine) seeds its
// installationId/generation here so the handover is accepted rather than
// permanently rejected as an identity conflict; see watcher-identity.json.
const localStore = new SessionStore(path.join(dataDir, 'collector-local-sessions.json'));
const savedLocalIdentity = selfObserve ? await optionalConfig('collector-local-identity.json') : null;
const legacyWatcherIdentity = selfObserve && !savedLocalIdentity ? await optionalConfig('watcher-identity.json') : null;
const localSeed = savedLocalIdentity ?? legacyWatcherIdentity;
const localIdentity = { installationId: localSeed?.installationId ?? randomUUID(),
  generation: (localSeed?.generation ?? 0) + 1, bootId: randomUUID() };
if (selfObserve) await saveConfig('collector-local-identity.json', localIdentity);
let processes = null, localReset = null, localLease = null, localSeq = 0, localPolling = false, localNotices = [];
const localObserver = selfObserve ? createLocalObserver(os.hostname(), await localStore.load(), () => processes,
  notice => localNotices.push(notice)) : null;
async function localPoll() {
  if (!selfObserve || localPolling || stopping) return;
  localPolling = true;
  try {
    await actions.run(async () => {
      if (!localLease) {
        await localObserver.monitor.update([], { healthy: false, reason: 'Collector connection changed; rebaselining' });
        const connected = await collector.connect({ version: 1, reporterId: localReporterId, ...localIdentity });
        localLease = connected.lease;
        localSeq = 0;
      }
      localNotices = [];
      let forcedGapReason = null;
      if (localReset) { forcedGapReason = localReset; localReset = null; }
      const outcome = await pollLocal(localObserver, forcedGapReason);
      await localStore.save(outcome.result.members);
      const payload = localReportPayload(localObserver.monitor, outcome, localNotices);
      await collector.accept({ version: 1, reporterId: localReporterId, lease: localLease,
        seq: ++localSeq, sentAt: new Date().toISOString(), ...payload });
    });
  } catch (error) {
    localLease = null;
    console.error(`Local self-observation unavailable (${error.status ? error.message : error.code ?? error.name})`);
  } finally { localPolling = false; }
}

function status() {
  const result = collector.snapshot();
  if (!bridgeReady || fault) {
    const mark = row => ({ ...row, state: 'unknown', finishedAt: null, dismissKey: null, runningCount: 0,
      detail: 'Collector unavailable; current status unconfirmed',
      ...(row.members ? { members: row.members.map(mark), relatives: row.relatives.map(mark) } : {}) });
    result.sessions = result.sessions.map(mark);
    result.members = result.members.map(mark);
    result.active = [];
    result.attention = result.sessions;
    result.issues.push(fault ?? 'Native helper unavailable');
  }
  return { ...result, instanceId, machine: os.hostname(), healthy: bridgeReady && !fault,
    source: 'Authenticated metadata reports from paired Windows watchers',
    updatedAt: new Date().toISOString(), notification,
    theme: bridgeReady && Date.now() - themeCheckedAt < 8000 ? theme
      : { mode: null, source: 'browser-fallback', reason: 'Windows theme reader unavailable' } };
}
async function testNotification() {
  if (Date.now() - lastTest < 5000) return;
  lastTest = Date.now();
  await notify(digest(`test:${randomUUID()}`), { kind: 'test', title: 'Copilot session monitor',
    message: 'TEST ONLY - notifications are delivered on the collector PC, independently of browsers and watchers.' });
}
const json = (res, code, value) => {
  res.writeHead(code, { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store' });
  res.end(JSON.stringify(value));
};
const assets = new Map([['/', ['index.html', 'text/html; charset=utf-8']],
  ['/app.js', ['app.js', 'text/javascript; charset=utf-8']], ['/style.css', ['style.css', 'text/css; charset=utf-8']]]);
const server = http.createServer(async (req, res) => {
  res.setHeader('X-Content-Type-Options', 'nosniff');
  res.setHeader('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'");
  if (![ `127.0.0.1:${port}`, `localhost:${port}`].includes(req.headers.host) ||
      req.headers.origin && ![url, `http://localhost:${port}`].includes(req.headers.origin) ||
      req.headers['sec-fetch-site'] === 'cross-site') return json(res, 403, { error: 'Local same-origin requests only' });
  try {
    if (req.method === 'GET' && req.url === '/api/status') json(res, 200, status());
    else if (req.method === 'GET' && req.url === '/api/control') json(res, 200, { token });
    else if (req.method === 'POST' && ['/api/test', '/api/stop', '/api/dismiss'].includes(req.url)) {
      if (req.headers.authorization !== `Bearer ${token}`) return json(res, 403, { error: 'Monitor control token required' });
      if (req.url === '/api/test') { await testNotification(); json(res, 200, { notification }); }
      else if (req.url === '/api/dismiss') json(res, 200, await actions.dismiss(await readDismissEntries(req)));
      else { json(res, 200, { stopping: true }); await stop(); }
    } else if (req.method === 'GET' && assets.has(req.url)) {
      const [file, type] = assets.get(req.url);
      res.writeHead(200, { 'Content-Type': type, 'Cache-Control': 'no-store' });
      res.end(await readFile(path.join(root, 'public', file)));
    } else json(res, 404, { error: 'Not found' });
  } catch (error) {
    console.error(`Local request failed (${error.code ?? error.name})`);
    if (!res.headersSent) json(res, error.status ?? 500, { error: error.status ? error.message : 'Collector request failed' });
    else res.end();
  }
});

async function ingest(req, res) {
  try {
    if (req.method !== 'POST' || !['/v1/connect', '/v1/report', '/v1/disconnect'].includes(req.url) ||
        req.headers.origin) throw fail('Reporter endpoint only', 403);
    await refreshConfiguration();
    const id = req.headers['x-monitor-reporter'];
    const pairing = collector.pairings.get(id);
    if (!pairing || !authorized(req.headers.authorization, pairing.tokenHash)) throw fail('Reporter authentication required', 403);
    const value = await readJson(req);
    const result = await actions.run(async () => {
      await refreshConfiguration();
      if (!collector.pairings.has(id)) throw fail('Reporter revoked', 403);
      if (req.url === '/v1/disconnect') {
        if (Object.keys(value).length !== 1 || !HASH.test(value.lease)) throw fail('Invalid disconnect');
        return collector.disconnect(id, value.lease);
      }
      if (value.reporterId !== id) throw fail('Reporter identity mismatch', 403);
      if (req.url === '/v1/connect') return collector.connect(value);
      return collector.accept(value);
    });
    json(res, 200, result);
  } catch (error) {
    console.error(`Reporter request rejected (${error.status ?? error.code ?? error.name})`);
    json(res, error.status ?? 503, { error: error.status ? error.message : 'Collector persistence/configuration unavailable' });
  }
}
const tls = { pfx: await readFile(path.join(dataDir, 'collector.pfx')), minVersion: 'TLSv1.2' };
const listeners = [];
for (const address of new Set(['127.0.0.1', config.bindAddress])) {
  const listener = https.createServer(tls, ingest);
  listener.requestTimeout = 6000;
  listener.headersTimeout = 5000;
  listener.maxConnections = 32;
  await new Promise((resolve, reject) => { listener.once('error', reject); listener.listen(config.port, address, resolve); });
  listeners.push(listener);
}
server.requestTimeout = 5000;
server.headersTimeout = 5000;
await new Promise((resolve, reject) => { server.once('error', reject); server.listen(port, '127.0.0.1', resolve); });
await saveConfig('runtime.json', { pid: process.pid, instanceId, url, token });
bridge = spawn(powershell, ['-NoLogo', '-NoProfile', '-NonInteractive', '-STA', '-ExecutionPolicy', 'Bypass',
  '-File', path.join(root, 'windows', 'tray.ps1'), '-MonitorUrl', url],
{ windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'] });
createInterface({ input: bridge.stdout }).on('line', async line => {
  try {
    const event = JSON.parse(line);
    if (event.type === 'ready') {
      bridgeReady = true;
      notification = { state: 'ready', message: 'Collector tray ready; use Test notification to check Windows delivery.' };
    } else if (event.type === 'theme') {
      themeCheckedAt = Date.now();
      theme = ['light', 'dark'].includes(event.mode) ? { mode: event.mode, source: 'windows-apps' }
        : { mode: null, source: 'browser-fallback', reason: 'Windows preference unavailable' };
    } else if (event.type === 'notification-shown' || event.type === 'notification-submitted') {
      if (event.id === notification.id) notification.state = event.type === 'notification-shown' || notification.state === 'shown' ? 'shown' : 'submitted';
    } else if (event.type === 'test') await testNotification();
    else if (event.type === 'stop') await stop();
    else if (event.type === 'generate-connection') {
      try {
        const label = typeof event.label === 'string' && event.label.trim() ? event.label.trim() : `Sub machine ${new Date().toISOString()}`;
        const value = await pairConnectionString(label);
        if (bridge.stdin.writable) bridge.stdin.write(`${JSON.stringify({ type: 'connection-string', value })}\n`);
      } catch (error) {
        console.error(`Connection request failed (${error.code ?? error.name})`);
        if (bridge.stdin.writable) bridge.stdin.write(`${JSON.stringify({ type: 'connect-result', ok: false, message: 'Could not generate a connection string' })}\n`);
      }
    }
    else if (event.type === 'power') {
      processes = null;
      localReset = 'Windows process observation interrupted; rebaselining';
      await actions.run(async () => {
        for (const source of collector.sources.values()) if (source.lease) collector.disconnect(source.id, source.lease);
      });
    } else if (event.type === 'processes') { processes = { at: Date.now(), processes: event.processes }; }
    else if (event.type === 'error') {
      processes = null;
      localReset = 'Windows process observation interrupted; rebaselining';
      throw new Error('Native helper error');
    }
  } catch (error) { fault = 'Native helper unavailable'; console.error(`${fault} (${error.name})`); }
});
bridge.stderr.on('data', () => console.error('Collector Windows helper reported an error'));
bridge.stdin.on('error', error => console.error(`Collector helper pipe failed (${error.code})`));
bridge.on('error', error => { bridgeReady = false; console.error(`Collector helper unavailable (${error.code})`); });
bridge.on('exit', () => { bridgeReady = false; processes = null; if (!stopping) console.error('Collector native helper stopped'); });
const timer = setInterval(() => {
  void actions.run(async () => {
    try { await refreshConfiguration(); collector.snapshot(); fault = null; }
    catch (error) { fault = 'Collector configuration unavailable'; console.error(`${fault} (${error.code ?? error.name})`); }
  });
  void localPoll();
}, 1500);
await localPoll();
async function stop() {
  if (stopping) return;
  stopping = true;
  clearInterval(timer);
  for (const listener of listeners) { listener.close(); listener.closeAllConnections(); }
  server.close();
  if (bridge.stdin.writable) bridge.stdin.end('{"type":"stop"}\n');
  while (localPolling) await new Promise(resolve => setTimeout(resolve, 50));
  await actions.run(async () => {
    if (localLease) { try { collector.disconnect(localReporterId, localLease); } catch { /* lease already invalid */ } }
    await collector.save(); await unlink(path.join(dataDir, 'runtime.json')); await release();
  });
  setTimeout(() => { if (bridge.exitCode === null) bridge.kill(); process.exit(0); }, 1200);
}
process.on('SIGINT', () => void stop());
process.on('SIGTERM', () => void stop());
console.log(`Copilot collector: ${url}. HTTPS reports: ${config.bindAddress}:${config.port}.`);
