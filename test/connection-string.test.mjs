import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, writeFile, readFile, rm } from 'node:fs/promises';
import { spawn, execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { DatabaseSync } from 'node:sqlite';
import { randomUUID, randomBytes } from 'node:crypto';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { CONNECTION_PREFIX, encodeConnectionString, decodeConnectionString } from '../src/protocol.mjs';

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
function samplePairing(certificate, overrides = {}) {
  return { version: 1, reporterId: randomUUID(), label: 'Synthetic sub machine',
    collectorUrl: 'https://127.0.0.1:45000', token: randomBytes(32).toString('hex'), certificate, ...overrides };
}
async function generateConnectionString(env, label) {
  const { stdout } = await execute(process.execPath, ['--input-type=module', '-e',
    `import { pairConnectionString } from './src/configuration.mjs'; console.log(await pairConnectionString(${JSON.stringify(label)}));`],
    { cwd: root, env });
  return stdout.trim();
}

test('connection string codec round-trips a valid pairing and rejects malformed/wrong-version input', {
  skip: process.platform !== 'win32', timeout: 30000,
}, async t => {
  const dir = await mkdtemp(path.join(os.tmpdir(), 'monitor-codec-fixture-'));
  t.after(() => rm(dir, { recursive: true, force: true }));
  const env = { ...process.env, MONITOR_DATA_DIR: dir };
  await execute(process.execPath, [path.join(root, 'src', 'configuration.mjs'), 'initialize', '127.0.0.1', String(await freePort())], { env });
  const certificate = await readFile(path.join(dir, 'collector-cert.pem'), 'utf8');
  const pairing = samplePairing(certificate);

  const value = encodeConnectionString(pairing);
  assert.ok(value.startsWith(CONNECTION_PREFIX));
  assert.deepEqual(decodeConnectionString(value), pairing);
  assert.deepEqual(decodeConnectionString(`  ${value}  `), pairing);

  assert.throws(() => decodeConnectionString(undefined), /Connection string is required/);
  assert.throws(() => decodeConnectionString('not-a-connection-string'), /Unrecognized connection string format/);
  assert.throws(() => decodeConnectionString(`${CONNECTION_PREFIX}${'a'.repeat(40000)}`), /too large/);
  assert.throws(() => decodeConnectionString(`${CONNECTION_PREFIX}not*valid*base64url!!`), /Malformed connection string/);
  const truncated = CONNECTION_PREFIX + Buffer.from(JSON.stringify(pairing).slice(0, 10), 'utf8').toString('base64url');
  assert.throws(() => decodeConnectionString(truncated), /Malformed connection string/);
  const wrongVersion = CONNECTION_PREFIX + Buffer.from(JSON.stringify({ ...pairing, version: 2 }), 'utf8').toString('base64url');
  assert.throws(() => decodeConnectionString(wrongVersion), /Invalid pairing/);
  const missingField = CONNECTION_PREFIX + Buffer.from(JSON.stringify({ ...pairing, token: undefined }), 'utf8').toString('base64url');
  assert.throws(() => decodeConnectionString(missingField), /Invalid pairing/);
});

test('host-mode "generate connection request" produces a string with the configured bind address/port and a fresh credential', {
  skip: process.platform !== 'win32', timeout: 30000,
}, async t => {
  const dir = await mkdtemp(path.join(os.tmpdir(), 'monitor-generate-fixture-'));
  t.after(() => rm(dir, { recursive: true, force: true }));
  const port = await freePort();
  const env = { ...process.env, MONITOR_DATA_DIR: dir };
  await execute(process.execPath, [path.join(root, 'src', 'configuration.mjs'), 'initialize', '127.0.0.1', String(port)], { env });

  const first = decodeConnectionString(await generateConnectionString(env, 'Second Windows PC'));
  assert.equal(first.collectorUrl, `https://127.0.0.1:${port}`);
  assert.equal(first.label, 'Second Windows PC');
  assert.match(first.token, /^[a-f0-9]{64}$/);

  const second = decodeConnectionString(await generateConnectionString(env, 'Second Windows PC'));
  assert.notEqual(second.reporterId, first.reporterId);
  assert.notEqual(second.token, first.token);

  const config = JSON.parse(await readFile(path.join(dir, 'collector.json'), 'utf8'));
  for (const pairing of [first, second]) {
    const entry = config.reporters.find(row => row.id === pairing.reporterId);
    assert.ok(entry, 'generated reporter must be recorded in collector.json');
    assert.equal(entry.legacy, false);
  }
});

test('child-mode "connect to host..." pastes a connection string into an unpaired watcher and begins reporting', {
  skip: process.platform !== 'win32', timeout: 60000,
}, async t => {
  const dir = await mkdtemp(path.join(os.tmpdir(), 'monitor-paste-connect-fixture-'));
  const hub = path.join(dir, 'hub'), watcherData = path.join(dir, 'watcher'), home = path.join(dir, 'home');
  const uiPort = await freePort(), ingestPort = await freePort();
  const owned = [];
  let output = '';
  const spawnOwned = (args, extra = {}) => {
    const child = spawn(process.execPath, args, { cwd: root, env: { ...process.env, ...extra }, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] });
    child.finished = new Promise(resolve => child.once('exit', resolve));
    for (const stream of [child.stdout, child.stderr]) stream.on('data', data => { output = (output + data).slice(-6000); });
    owned.push(child);
    return child;
  };
  t.after(async () => {
    for (const child of owned) if (child.exitCode === null) child.kill();
    await Promise.all(owned.map(child => child.finished));
    await pause(1600);
    await rm(dir, { recursive: true, force: true });
  });

  const hubEnv = { MONITOR_DATA_DIR: hub, MONITOR_PORT: String(uiPort) };
  await execute(process.execPath, [path.join(root, 'src', 'configuration.mjs'), 'initialize', '127.0.0.1', String(ingestPort)], { env: { ...process.env, ...hubEnv } });
  const collector = spawnOwned(['--disable-warning=ExperimentalWarning', path.join(root, 'src', 'server.mjs')], hubEnv);
  const uiUrl = `http://127.0.0.1:${uiPort}`;
  async function waitForCollector(predicate, description) {
    const start = Date.now();
    while (Date.now() - start < 15000) {
      let state;
      try { state = await (await fetch(`${uiUrl}/api/status`, { signal: AbortSignal.timeout(3000) })).json(); }
      catch (error) { if (error.cause?.code !== 'ECONNREFUSED') throw error; }
      if (state && predicate(state)) return state;
      assert.equal(collector.exitCode, null, output);
      await pause(150);
    }
    assert.fail(`${description}: ${output}`);
  }
  await waitForCollector(state => state.healthy, 'Collector startup');

  await mkdir(watcherData, { recursive: true });
  await mkdir(path.join(home, '.copilot', 'session-state'), { recursive: true });
  const db = new DatabaseSync(path.join(home, '.copilot', 'data.db'));
  db.exec(`CREATE TABLE sessions(id,title,is_running,was_interrupted,execution_location,session_type,archived_at);
    CREATE TABLE workspaces(id,session_id,creator_session_id,host_id,archived_at);
    CREATE TABLE workspace_parent_links(child_workspace_id,parent_workspace_id);
    CREATE TABLE workspace_session_aliases(session_id,workspace_id);
    CREATE TABLE workspace_side_chats(workspace_id,session_id);
    CREATE TABLE session_side_chats(parent_session_id,session_id);`);
  db.close();
  const watcherEnv = { USERPROFILE: home, MONITOR_DATA_DIR: watcherData };
  const watcher = spawnOwned(['--disable-warning=ExperimentalWarning', path.join(root, 'src', 'watcher.mjs')], watcherEnv);
  async function waitForWatcherStatus(runtimeUrl, predicate, description) {
    const start = Date.now();
    while (Date.now() - start < 15000) {
      let state;
      try { state = await (await fetch(`${runtimeUrl}/status`, { signal: AbortSignal.timeout(3000) })).json(); }
      catch (error) { if (error.cause?.code !== 'ECONNREFUSED') throw error; }
      if (state && predicate(state)) return state;
      assert.equal(watcher.exitCode, null, output);
      await pause(150);
    }
    assert.fail(`${description}: ${output}`);
  }
  for (let i = 0; i < 60 && !(await readFileSafe(path.join(watcherData, 'watcher-runtime.json'))); i++) await pause(150);
  const runtime = JSON.parse(await readFile(path.join(watcherData, 'watcher-runtime.json'), 'utf8'));
  const unpaired = await waitForWatcherStatus(runtime.url, state => state.reporterId === null, 'Unpaired watcher startup');
  assert.equal(unpaired.healthy, false);
  assert.match(unpaired.issue, /Waiting to be paired/);

  const postConnect = value => fetch(`${runtime.url}/connect`, { method: 'POST',
    body: JSON.stringify({ value }), headers: { Authorization: `Bearer ${runtime.token}` }, signal: AbortSignal.timeout(5000) });

  assert.equal((await postConnect('csm1:not-valid-base64url-payload!!')).status, 400);

  const connectionString = await generateConnectionString({ ...process.env, ...hubEnv }, 'Real watcher fixture');
  const expectedReporterId = decodeConnectionString(connectionString).reporterId;
  const connectResult = await (await postConnect(connectionString)).json();
  assert.equal(connectResult.ok, true);
  assert.equal(connectResult.label, 'Real watcher fixture');
  assert.equal(connectResult.host, `127.0.0.1:${ingestPort}`);

  const savedPairing = JSON.parse(await readFile(path.join(watcherData, 'watcher.json'), 'utf8'));
  assert.equal(savedPairing.reporterId, expectedReporterId);

  await waitForCollector(state => state.sources.find(source => source.id === expectedReporterId)?.healthy, 'Paired watcher reporting');
  await waitForWatcherStatus(runtime.url, state => state.reporterId === expectedReporterId && state.healthy, 'Watcher status reflects pairing');

  // Re-pairing to a *different* reporter identity while already paired must be rejected.
  const otherConnectionString = await generateConnectionString({ ...process.env, ...hubEnv }, 'Different sub machine');
  const rejected = await (await postConnect(otherConnectionString)).json();
  assert.equal(rejected.ok, false);
  assert.match(rejected.message, /already paired with a different host/);
  const unchangedPairing = JSON.parse(await readFile(path.join(watcherData, 'watcher.json'), 'utf8'));
  assert.equal(unchangedPairing.reporterId, expectedReporterId);
});

async function readFileSafe(file) {
  try { return await readFile(file); } catch { return null; }
}
