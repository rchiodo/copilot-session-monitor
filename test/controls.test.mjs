import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, writeFile, readFile, rm, readdir } from 'node:fs/promises';
import { spawn, execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { DatabaseSync } from 'node:sqlite';
import { randomUUID, randomBytes } from 'node:crypto';
import net from 'node:net';
import https from 'node:https';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { request as reportRequest, digest, MAX_BYTES } from '../src/protocol.mjs';

const execute = promisify(execFile);
const root = fileURLToPath(new URL('..', import.meta.url));
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
async function freePort() {
  const reservation = net.createServer();
  await new Promise(resolve => reservation.listen(0, '127.0.0.1', resolve));
  const port = reservation.address().port;
  await new Promise(resolve => reservation.close(resolve));
  assert.notEqual(port, 43187);
  return port;
}
const rawMember = (state, id = 'parent', parentId = null) => {
  const at = new Date().toISOString();
  return { id, parentId, title: `Synthetic ${id}`, source: 'Copilot desktop', state,
    detail: 'Synthetic lifecycle state', activity: 'Executing tools', runId: `run-${id}`,
    firstObservedAt: at, startedAt: at, lastEventAt: at, lastResponseAt: at,
    finishedAt: state === 'finished' ? at : null, hierarchyIssue: null, contextOnly: false, lastAlert: null,
    completionTracked: state === 'working' };
};

test('isolated HTTPS collector and watcher processes: TLS/auth, lifecycle, offline, migration, controls and restart', {
  skip: process.platform !== 'win32', timeout: 120000,
}, async t => {
  const dir = await mkdtemp(path.join(os.tmpdir(), 'monitor-network-fixture-'));
  const hub = path.join(dir, 'hub');
  const uiPort = await freePort(), ingestPort = await freePort();
  const env = { ...process.env, MONITOR_DATA_DIR: hub, MONITOR_PORT: String(uiPort) };
  const owned = [];
  let server, output = '';
  const spawnOwned = (args, extra = {}, ipc = false) => {
    const child = spawn(process.execPath, args, { cwd: root, env: { ...env, ...extra }, windowsHide: true,
      stdio: ipc ? ['ignore', 'pipe', 'pipe', 'ipc'] : ['ignore', 'pipe', 'pipe'] });
    child.finished = new Promise(resolve => child.once('exit', resolve));
    for (const stream of [child.stdout, child.stderr]) stream.on('data', data => { output = (output + data).slice(-6000); });
    owned.push(child);
    return child;
  };
  t.after(async () => {
    for (const child of owned) if (child.exitCode === null) child.kill();
    await Promise.all(owned.map(child => child.finished));
    // The native helpers exit on the owning process pipe closing.
    await pause(1600);
    await rm(dir, { recursive: true, force: true });
  });
  await execute(process.execPath, [path.join(root, 'src', 'configuration.mjs'), 'initialize', '127.0.0.1', String(ingestPort)], { env });
  const configFile = path.join(hub, 'collector.json');
  const config = JSON.parse(await readFile(configFile));
  const certificate = await readFile(path.join(hub, 'collector-cert.pem'), 'utf8');
  const pairings = [0, 1, 2].map(index => ({ version: 1, reporterId: randomUUID(),
    label: index === 2 ? 'Real watcher fixture' : 'Duplicate hostname', token: randomBytes(32).toString('hex'),
    collectorUrl: `https://127.0.0.1:${ingestPort}`, certificate }));
  config.reporters = pairings.map((pairing, index) => ({ id: pairing.reporterId, label: pairing.label,
    tokenHash: digest(pairing.token), legacy: index === 0 }));
  await writeFile(configFile, JSON.stringify(config));
  const old = { ...rawMember('finished'), machine: 'OLD-SYNTHETIC' };
  const { groupFamilies } = await import('../src/families.mjs');
  const oldKey = groupFamilies([old])[0].dismissKey;
  await writeFile(path.join(hub, 'sessions.json'), JSON.stringify({ version: 3,
    sessions: [old], dismissed: [{ id: old.id, key: oldKey }] }));
  const originalKeys = ['b'.repeat(64)];
  await writeFile(path.join(hub, 'notifications.json'), JSON.stringify({ version: 1, keys: originalKeys }));
  const url = `http://127.0.0.1:${uiPort}`;
  const get = async () => (await fetch(`${url}/api/status`, { signal: AbortSignal.timeout(3000) })).json();
  async function waitFor(predicate, description, timeout = 15000) {
    const start = Date.now();
    while (Date.now() - start < timeout) {
      let result;
      try { result = await get(); } catch (error) {
        if (error.cause?.code !== 'ECONNREFUSED') throw error;
      }
      if (result && predicate(result)) return result;
      if (server) assert.equal(server.exitCode, null, output);
      await pause(150);
    }
    assert.fail(`${description}: ${output}`);
  }
  const start = async () => {
    server = spawnOwned(['--disable-warning=ExperimentalWarning', path.join(root, 'src', 'server.mjs')]);
    return waitFor(state => state.healthy, 'Collector startup');
  };
  const localPost = async (route, body, headers = {}) => {
    const token = (await (await fetch(`${url}/api/control`)).json()).token;
    return fetch(`${url}${route}`, { method: 'POST', body: JSON.stringify(body),
      headers: { Authorization: `Bearer ${token}`, ...headers }, signal: AbortSignal.timeout(5000) });
  };
  const stop = async () => {
    assert.equal((await localPost('/api/stop')).status, 200);
    await server.finished;
  };
  const initial = await start();
  assert.equal(initial.sessions[0].state, 'unknown');
  assert.equal(initial.sessions[0].dismissKey, null);
  assert.equal(initial.sources.length, 3);
  assert.ok((await readdir(hub)).some(name => name.startsWith('backup-')));
  assert.deepEqual(JSON.parse(await readFile(path.join(hub, 'notifications.json'))).keys, originalKeys);
  assert.equal((await fetch(`${url}/api/dismiss`)).status, 404);
  assert.equal((await fetch(`${url}/api/dismiss`, { method: 'POST' })).status, 403);
  assert.equal((await localPost('/api/dismiss', {}, { Origin: 'https://invalid.example' })).status, 403);
  assert.equal((await localPost('/api/dismiss', { entries: [] })).status, 400);
  assert.equal(await new Promise((resolve, reject) => {
    http.get(`${url}/api/status`, { headers: { Host: 'invalid.example' } }, res => {
      res.resume(); res.on('end', () => resolve(res.statusCode));
    }).on('error', reject);
  }), 403);

  const identity = { version: 1, reporterId: pairings[0].reporterId,
    installationId: randomUUID(), bootId: randomUUID(), generation: 1 };
  await assert.rejects(reportRequest({ ...pairings[0], token: '0'.repeat(64) }, '/v1/connect', identity), error => error.status === 403);
  await assert.rejects(reportRequest(pairings[0], '/api/control', {}), error => error.status === 403);
  await assert.rejects(reportRequest(pairings[0], '/v1/connect', { ...identity, reporterId: pairings[1].reporterId }), error => error.status === 403);
  await assert.rejects(reportRequest(pairings[0], '/v1/connect', { ...identity, version: 2 }), error => error.status === 400);
  const tlsResult = await new Promise(resolve => {
    const req = https.get(`${pairings[0].collectorUrl}/v1/connect`, res => { res.resume(); resolve('unexpected success'); });
    req.on('error', error => resolve(error.code));
  });
  assert.match(tlsResult, /SELF_SIGNED|UNABLE_TO_VERIFY/);
  const nameResult = await new Promise(resolve => {
    const req = https.get(`${pairings[0].collectorUrl}/v1/connect`,
      { ca: certificate, servername: 'wrong.invalid.example' }, res => { res.resume(); resolve('unexpected success'); });
    req.on('error', error => resolve(error.code));
  });
  assert.equal(nameResult, 'ERR_TLS_CERT_ALTNAME_INVALID');
  const oversized = await new Promise((resolve, reject) => {
    const req = https.request(`${pairings[0].collectorUrl}/v1/report`, { method: 'POST', ca: certificate,
      headers: { Authorization: `Bearer ${pairings[0].token}`, 'X-Monitor-Reporter': pairings[0].reporterId,
        'Content-Type': 'application/json', 'Content-Length': MAX_BYTES + 1 } },
    res => { res.resume(); res.on('end', () => resolve(res.statusCode)); });
    req.on('error', reject); req.end('{}');
  });
  assert.equal(oversized, 413);

  const workers = [];
  const workerCode = `
    import { readFile } from 'node:fs/promises';
    import { randomUUID } from 'node:crypto';
    import { Reporter } from './src/reporter.mjs';
    const client = new Reporter(JSON.parse(await readFile(process.env.FIXTURE_PAIRING)),
      { installationId: randomUUID(), bootId: randomUUID(), generation: 1 });
    process.on('message', async message => {
      try {
        if (message.action === 'connect') await client.connect();
        else if (message.action === 'report') await client.send(message.snapshot);
        else if (message.action === 'disconnect') await client.disconnect();
        process.send({ ok: true });
      } catch (error) { process.send({ ok: false, status: error.status, error: error.message }); }
    });
    process.send({ ready: true });
  `;
  const command = (child, message) => new Promise((resolve, reject) => {
    const timeout = setTimeout(() => { child.off('message', receive); reject(new Error('Fixture worker timed out')); }, 12000);
    function receive(value) { clearTimeout(timeout); assert.equal(value.ok, true, JSON.stringify(value)); resolve(value); }
    child.once('message', receive);
    child.send(message);
  });
  for (let index = 0; index < 2; index++) {
    const file = path.join(dir, `fixture-pairing-${index}.json`);
    await writeFile(file, JSON.stringify(pairings[index]));
    const child = spawnOwned(['--input-type=module', '-e', workerCode], { FIXTURE_PAIRING: file }, true);
    await new Promise(resolve => child.once('message', resolve));
    workers.push(child);
    await command(child, { action: 'connect' });
    await command(child, { action: 'report', snapshot: { healthy: true, issues: [], relatives: [], notices: [],
      members: [rawMember('working'), rawMember('working', 'child', 'parent')] } });
  }
  let view = await get();
  assert.equal(view.active.length, 2);
  assert.notEqual(view.active[0].id, view.active[1].id);
  assert.notEqual(view.active[0].machineTag, view.active[1].machineTag);
  const finished = [rawMember('finished'), rawMember('finished', 'child', 'parent')];
  await command(workers[0], { action: 'report', snapshot: { healthy: true, issues: [], relatives: [],
    notices: [{ kind: 'finished', familyId: 'parent', key: digest('synthetic-finish') }], members: finished } });
  view = await get();
  assert.equal(view.active.length, 1);
  assert.equal(view.notification.kind, 'finished');
  const row = view.sessions.find(row => row.state === 'finished');
  const entry = { id: row.id, key: row.dismissKey };
  assert.equal((await (await localPost('/api/dismiss', { entries: [entry] })).json()).dismissed.length, 1);
  await command(workers[0], { action: 'report', snapshot: { healthy: true, issues: [], relatives: [], notices: [],
    members: [finished[0], { ...rawMember('working', 'child', 'parent'), runId: 'resumed-child' }] } });
  assert.equal((await get()).active.length, 2);
  assert.equal((await (await localPost('/api/dismiss', { entries: [entry] })).json()).skipped.length, 1);
  const savedKeys = JSON.parse(await readFile(path.join(hub, 'notifications.json'))).keys;
  assert.equal(savedKeys.length, 2);

  // Exercise the full watcher executable against an empty, synthetic Copilot home.
  const watcherData = path.join(dir, 'watcher'), home = path.join(dir, 'home');
  await mkdir(watcherData);
  await writeFile(path.join(watcherData, 'watcher.json'), JSON.stringify(pairings[2]));
  await mkdir(path.join(home, '.copilot', 'session-state'), { recursive: true });
  const db = new DatabaseSync(path.join(home, '.copilot', 'data.db'));
  db.exec(`CREATE TABLE sessions(id,title,is_running,was_interrupted,execution_location,session_type,archived_at);
    CREATE TABLE workspaces(id,session_id,creator_session_id,host_id,archived_at);
    CREATE TABLE workspace_parent_links(child_workspace_id,parent_workspace_id);
    CREATE TABLE workspace_session_aliases(session_id,workspace_id);
    CREATE TABLE workspace_side_chats(workspace_id,session_id);
    CREATE TABLE session_side_chats(parent_session_id,session_id);`);
  db.close();
  const watcher = spawnOwned(['--disable-warning=ExperimentalWarning', path.join(root, 'src', 'watcher.mjs')],
    { USERPROFILE: home, MONITOR_DATA_DIR: watcherData });
  await waitFor(state => state.sources.find(source => source.id === pairings[2].reporterId)?.healthy, 'Real watcher executable');
  const runtime = JSON.parse(await readFile(path.join(watcherData, 'watcher-runtime.json')));
  assert.equal((await fetch(`${runtime.url}/stop`, { method: 'POST' })).status, 403);
  await fetch(`${runtime.url}/stop`, { method: 'POST', headers: { Authorization: `Bearer ${runtime.token}` } });
  await watcher.finished;
  const firstIdentity = JSON.parse(await readFile(path.join(watcherData, 'watcher-identity.json')));
  assert.equal((await get()).sources.find(source => source.id === pairings[2].reporterId).healthy, false);
  const resumedWatcher = spawnOwned(['--disable-warning=ExperimentalWarning', path.join(root, 'src', 'watcher.mjs')],
    { USERPROFILE: home, MONITOR_DATA_DIR: watcherData });
  await waitFor(state => state.sources.find(source => source.id === pairings[2].reporterId)?.healthy, 'Restarted watcher executable');
  const resumedIdentity = JSON.parse(await readFile(path.join(watcherData, 'watcher-identity.json')));
  assert.equal(resumedIdentity.installationId, firstIdentity.installationId);
  assert.equal(resumedIdentity.generation, firstIdentity.generation + 1);
  assert.deepEqual(JSON.parse(await readFile(path.join(hub, 'notifications.json'))).keys, savedKeys);
  const resumedRuntime = JSON.parse(await readFile(path.join(watcherData, 'watcher-runtime.json')));
  assert.equal((await fetch(`${resumedRuntime.url}/stop`, { method: 'POST',
    headers: { Authorization: `Bearer ${resumedRuntime.token}` } })).status, 200);
  await resumedWatcher.finished;
  const oldIdentity = JSON.parse(await readFile(path.join(watcherData, 'watcher-identity.json')));
  const handshake = { version: 1, reporterId: pairings[2].reporterId, ...oldIdentity,
    bootId: randomUUID(), generation: oldIdentity.generation + 1 };
  const lease = await reportRequest(pairings[2], '/v1/connect', handshake);
  const packet = { version: 1, reporterId: pairings[2].reporterId, lease: lease.lease, seq: 1,
    sentAt: new Date().toISOString(), healthy: true, issues: [], members: [], relatives: [], notices: [] };
  await reportRequest(pairings[2], '/v1/report', packet);
  assert.equal((await reportRequest(pairings[2], '/v1/report', packet)).duplicate, true);
  await assert.rejects(reportRequest(pairings[2], '/v1/report', { ...packet, seq: 3 }), error => error.status === 409);
  await assert.rejects(reportRequest(pairings[2], '/v1/report', { ...packet, seq: 2, prompt: 'Not metadata' }), error => error.status === 400);
  await reportRequest(pairings[2], '/v1/connect', handshake);
  await assert.rejects(reportRequest(pairings[2], '/v1/report', packet), error => error.status === 409);
  view = await waitFor(state => state.sources.every(source => !source.healthy), 'Heartbeat expiry for silent synthetic watchers', 18000);
  assert.equal(view.sessions.every(row => row.state === 'unknown' && row.dismissKey === null), true);
  assert.deepEqual(JSON.parse(await readFile(path.join(hub, 'notifications.json'))).keys, savedKeys);
  await stop();
  await start();
  await command(workers[0], { action: 'connect' });
  await command(workers[0], { action: 'report', snapshot: { healthy: true, issues: [], relatives: [],
    notices: [{ kind: 'finished', familyId: 'parent', key: digest('synthetic-finish') }], members: finished } });
  assert.equal((await get()).notification.state, 'ready');
  assert.deepEqual(JSON.parse(await readFile(path.join(hub, 'notifications.json'))).keys, savedKeys);
  assert.equal((await get()).members.find(row => row.id.endsWith('~parent') && row.reporterId === pairings[0].reporterId).firstObservedAt, old.firstObservedAt);
  config.reporters = config.reporters.filter(row => row.id !== pairings[1].reporterId);
  await writeFile(configFile, JSON.stringify(config));
  await assert.rejects(reportRequest(pairings[1], '/v1/connect', identity), error => error.status === 403);
  await stop();
});
