import path from 'node:path';
import http from 'node:http';
import { randomUUID, randomBytes } from 'node:crypto';
import { mkdir, unlink } from 'node:fs/promises';
import { spawn } from 'node:child_process';
import { createInterface } from 'node:readline';
import { root, dataDir, powershell, readConfig, optionalConfig, saveConfig } from './configuration.mjs';
import { acquireRole } from './lifecycle.mjs';
import { SessionStore } from './engine.mjs';
import { decodeConnectionString } from './protocol.mjs';
import { createLocalObserver, pollLocal, localReportPayload } from './local-report.mjs';
import { Reporter } from './reporter.mjs';

if (process.platform !== 'win32') throw new Error('The watcher requires Windows process evidence.');
await mkdir(dataDir, { recursive: true });
const release = await acquireRole(dataDir, 'watcher');
let pairing = await optionalConfig('watcher.json');
const savedIdentity = await optionalConfig('watcher-identity.json');
const identity = { installationId: savedIdentity?.installationId ?? randomUUID(),
  generation: (savedIdentity?.generation ?? 0) + 1, bootId: randomUUID() };
await saveConfig('watcher-identity.json', identity);
const store = new SessionStore(path.join(dataDir, 'watcher-sessions.json'));
let reporter = null, monitor = null, source = null, timer = null;
let notices = [], processes = null, reset = null, stopping = false, polling = false, lastError = null;
let health = pairing
  ? { healthy: false, issue: 'Starting watcher', lastAcknowledgedAt: null }
  : { healthy: false, issue: 'Waiting to be paired with a host (use the tray "Connect to host..." menu)', lastAcknowledgedAt: null };
const controlToken = randomBytes(32).toString('hex');
let port;
const control = http.createServer((req, res) => {
  res.setHeader('Cache-Control', 'no-store');
  res.setHeader('Content-Type', 'application/json');
  if (req.headers.host !== `127.0.0.1:${port}` || req.headers.origin || req.headers['sec-fetch-site'] === 'cross-site') {
    res.writeHead(403); return res.end('{}');
  }
  if (req.method === 'GET' && req.url === '/status') {
    return res.end(JSON.stringify({ ...health, instanceId: identity.bootId, reporterId: pairing?.reporterId ?? null }));
  }
  if (req.method === 'POST' && req.url === '/connect' && req.headers.authorization === `Bearer ${controlToken}`) {
    let body = '';
    req.on('data', chunk => { body += chunk; if (body.length > 65536) req.destroy(); });
    req.on('end', () => {
      void (async () => {
        try {
          const { value } = JSON.parse(body);
          const result = await applyConnectionString(value);
          res.end(JSON.stringify({ ok: true, ...result }));
        } catch (error) {
          res.writeHead(error.status ?? 400);
          res.end(JSON.stringify({ ok: false, message: error.message }));
        }
      })();
    });
    return;
  }
  if (req.method === 'POST' && req.url === '/stop' && req.headers.authorization === `Bearer ${controlToken}`) {
    res.end('{"stopping":true}');
    void stop();
  } else { res.writeHead(403); res.end('{}'); }
});
await new Promise((resolve, reject) => { control.once('error', reject); control.listen(0, '127.0.0.1', resolve); });
port = control.address().port;
await saveConfig('watcher-runtime.json', { pid: process.pid, instanceId: identity.bootId,
  url: `http://127.0.0.1:${port}`, token: controlToken });

const bridge = spawn(powershell, ['-NoLogo', '-NoProfile', '-NonInteractive', '-STA', '-ExecutionPolicy', 'Bypass',
  '-File', path.join(root, 'windows', 'tray.ps1'), '-WatcherOnly'],
{ windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'] });
createInterface({ input: bridge.stdout }).on('line', line => {
  try {
    const event = JSON.parse(line);
    if (event.type === 'processes') processes = { at: Date.now(), processes: event.processes };
    if (event.type === 'power' || event.type === 'error') {
      reset = 'Windows process observation interrupted; rebaselining';
      processes = null;
    }
    if (event.type === 'connect') {
      void applyConnectionString(event.value).then(result => {
        if (bridge.stdin.writable) bridge.stdin.write(`${JSON.stringify({ type: 'connect-result', ok: true, ...result })}\n`);
      }).catch(error => {
        if (bridge.stdin.writable) bridge.stdin.write(`${JSON.stringify({ type: 'connect-result', ok: false, message: error.message })}\n`);
      });
    }
  } catch {
    processes = null;
    reset = 'Invalid Windows helper status; rebaselining';
    console.error(reset);
  }
});
bridge.stderr.on('data', () => { console.error('Watcher Windows helper reported an error'); processes = null; });
bridge.on('error', error => { console.error(`Watcher helper unavailable (${error.code})`); processes = null; });
bridge.on('exit', () => { processes = null; reset = 'Watcher helper stopped'; });
bridge.stdin.on('error', error => console.error(`Watcher helper pipe unavailable (${error.code})`));

async function poll() {
  if (polling || stopping || !reporter) return;
  polling = true;
  try {
    if (!reporter.lease) {
      await monitor.update([], { healthy: false, reason: 'Collector connection changed; rebaselining' });
      await reporter.connect();
    }
    notices = [];
    let forcedGapReason = null;
    if (reset) { forcedGapReason = reset; reset = null; }
    const outcome = await pollLocal({ source, monitor }, forcedGapReason);
    await store.save(outcome.result.members);
    const response = await reporter.send(localReportPayload(monitor, outcome, notices));
    health = { healthy: outcome.healthy && response.healthy !== false,
      issue: outcome.healthy ? null : outcome.issues.join('; '), lastAcknowledgedAt: new Date().toISOString() };
    lastError = null;
  } catch (error) {
    reporter.lease = null;
    health = { ...health, healthy: false, issue: error.status
      ? error.message : `Reporting unavailable (${error.code ?? error.name})` };
    if (health.issue !== lastError) console.error(health.issue);
    lastError = health.issue;
  } finally { polling = false; }
}
async function beginReporting(nextPairing) {
  if (timer) { clearInterval(timer); timer = null; }
  if (reporter) { try { await reporter.disconnect(); } catch (error) { console.error(`Collector disconnect not acknowledged (${error.status ?? error.code})`); } }
  pairing = nextPairing;
  reporter = new Reporter(pairing, identity);
  ({ monitor, source } = createLocalObserver(pairing.label, await store.load(), () => processes,
    notice => notices.push(notice)));
  notices = [];
  health = { healthy: false, issue: 'Starting watcher', lastAcknowledgedAt: null };
  timer = setInterval(poll, 1500);
  await poll();
}

async function applyConnectionString(value) {
  const nextPairing = decodeConnectionString(value);
  if (pairing && pairing.reporterId !== nextPairing.reporterId) {
    throw new Error('This watcher is already paired with a different host; use a separate installation directory');
  }
  await saveConfig('watcher.json', nextPairing);
  await beginReporting(nextPairing);
  return { label: nextPairing.label, host: new URL(nextPairing.collectorUrl).host };
}

if (pairing) await beginReporting(pairing);
async function stop() {
  if (stopping) return;
  stopping = true;
  clearInterval(timer);
  while (polling) await new Promise(resolve => setTimeout(resolve, 50));
  if (reporter) { try { await reporter.disconnect(); } catch (error) { console.error(`Collector disconnect not acknowledged (${error.status ?? error.code})`); } }
  if (bridge.stdin.writable) bridge.stdin.end('{"type":"stop"}\n');
  control.close();
  await unlink(path.join(dataDir, 'watcher-runtime.json'));
  await release();
  setTimeout(() => { if (bridge.exitCode === null) bridge.kill(); process.exit(0); }, 1200);
}
process.on('SIGINT', () => void stop());
process.on('SIGTERM', () => void stop());
console.log('Copilot watcher running; metadata reports use the paired HTTPS collector.');
