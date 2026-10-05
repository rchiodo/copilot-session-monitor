import { spawn } from 'node:child_process';
import { mkdtemp, readFile, rm, access } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';

const root = fileURLToPath(new URL('..', import.meta.url));
const profile = await mkdtemp(path.join(os.tmpdir(), 'monitor-browser-fixture-'));
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
let browser, fixture, socket;
const pending = new Map();
let nextId = 0;
function call(method, params = {}) {
  return new Promise((resolve, reject) => {
    const id = ++nextId;
    const timer = setTimeout(() => { pending.delete(id); reject(new Error(`Browser command timed out: ${method}`)); }, 15000);
    pending.set(id, value => { clearTimeout(timer); value.error ? reject(new Error(value.error.message)) : resolve(value.result); });
    socket.send(JSON.stringify({ id, method, params }));
  });
}
try {
  fixture = spawn(process.execPath, [path.join(root, 'scripts', 'verify-ui.mjs')], { cwd: root, windowsHide: true });
  fixture.stderr.pipe(process.stderr);
  const url = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('Fixture startup timed out')), 10000);
    fixture.stdout.on('data', data => {
      const match = /http:\/\/127\.0\.0\.1:\d+/.exec(data.toString());
      if (match) { clearTimeout(timer); resolve(match[0]); }
    });
    fixture.on('error', reject);
  });
  let edge;
  for (const base of [process.env['ProgramFiles(x86)'], process.env.ProgramFiles]) {
    if (!base) continue;
    const candidate = path.join(base, 'Microsoft', 'Edge', 'Application', 'msedge.exe');
    try { await access(candidate); edge = candidate; break; }
    catch (error) { if (error.code !== 'ENOENT') throw error; }
  }
  if (!edge) throw new Error('Installed Microsoft Edge is required; no browser is downloaded');
  browser = spawn(edge, ['--headless=new', '--remote-debugging-port=0', `--user-data-dir=${profile}`,
    '--no-first-run', '--no-default-browser-check', '--disable-background-networking', '--disable-sync',
    '--window-size=1200,900', 'about:blank'], { windowsHide: true, stdio: 'ignore' });
  let debugPort;
  for (let attempt = 0; attempt < 100; attempt++) {
    try { debugPort = Number((await readFile(path.join(profile, 'DevToolsActivePort'), 'utf8')).split('\n')[0]); break; }
    catch (error) { if (error.code !== 'ENOENT') throw error; }
    await pause(100);
  }
  if (!debugPort) throw new Error('Owned headless browser did not start');
  const pages = await (await fetch(`http://127.0.0.1:${debugPort}/json/list`)).json();
  socket = new WebSocket(pages.find(page => page.type === 'page').webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { socket.addEventListener('open', resolve, { once: true }); socket.addEventListener('error', reject, { once: true }); });
  socket.addEventListener('message', event => {
    const value = JSON.parse(event.data);
    if (pending.has(value.id)) { pending.get(value.id)(value); pending.delete(value.id); }
  });
  await call('Emulation.setDeviceMetricsOverride', { width: 1200, height: 900, deviceScaleFactor: 1, mobile: false });
  await call('Page.navigate', { url });
  let results = [];
  for (let attempt = 0; attempt < 120 && results.length < 10; attempt++) {
    await pause(1500);
    results = await (await fetch(`${url}/results`)).json();
  }
  assert.equal(results.length, 10, 'Expected five viewports in both themes');
  console.log(JSON.stringify(results.map(row => ({ viewport: row.viewport, mode: row.mode, passed: row.passed,
    running: row.running, retained: row.retained, failures: Object.entries(row.checks ?? {}).filter(([, ok]) => !ok).map(([name]) => name),
    ...(row.error ? { error: row.error } : {}) })), null, 2));
  assert.ok(results.every(row => row.passed), 'Browser fixture checks failed');
  if (process.argv.includes('--live')) {
    await call('Page.navigate', { url: 'http://127.0.0.1:43187' });
    await pause(3000);
    const result = await call('Runtime.evaluate', { returnByValue: true, expression: `({
      sourceCoverage: document.querySelector('#source-summary').textContent,
      sourcesVisible: !document.querySelector('#source-health').hidden,
      cards: document.querySelectorAll('.card').length,
      machineLabels: [...document.querySelectorAll('.row-title')].every(node => node.textContent.startsWith('[')),
      theme: document.documentElement.dataset.theme,
      overflow: document.documentElement.scrollWidth > innerWidth,
      connected: document.querySelector('#health').className === 'connected'
    })` });
    console.log(JSON.stringify({ live: result.result.value }, null, 2));
    assert.equal(result.result.value.sourcesVisible, true);
    assert.equal(result.result.value.machineLabels, true);
    assert.equal(result.result.value.overflow, false);
    assert.equal(result.result.value.connected, true);
  }
} finally {
  if (socket?.readyState === WebSocket.OPEN) await call('Browser.close');
  if (browser) {
    for (let i = 0; i < 50 && browser.exitCode === null; i++) await pause(100);
    if (browser.exitCode === null) browser.kill();
  }
  if (fixture && fixture.exitCode === null) {
    const exited = new Promise(resolve => fixture.once('exit', resolve));
    fixture.kill();
    await exited;
  }
  await pause(1000);
  await rm(profile, { recursive: true, force: true });
}
