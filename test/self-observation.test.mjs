import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, writeFile, readFile, rm, readdir } from 'node:fs/promises';
import { spawn, execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { DatabaseSync } from 'node:sqlite';
import { randomUUID } from 'node:crypto';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { decodeConnectionString, digest } from '../src/protocol.mjs';
import { Reporter } from '../src/reporter.mjs';
import { groupFamilies } from '../src/families.mjs';

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
async function emptyCopilotHome(home) {
  await mkdir(path.join(home, '.copilot', 'session-state'), { recursive: true });
  const db = new DatabaseSync(path.join(home, '.copilot', 'data.db'));
  db.exec(`CREATE TABLE sessions(id,title,is_running,was_interrupted,execution_location,session_type,archived_at);
    CREATE TABLE workspaces(id,session_id,creator_session_id,host_id,archived_at);
    CREATE TABLE workspace_parent_links(child_workspace_id,parent_workspace_id);
    CREATE TABLE workspace_session_aliases(session_id,workspace_id);
    CREATE TABLE workspace_side_chats(workspace_id,session_id);
    CREATE TABLE session_side_chats(parent_session_id,session_id);`);
  db.close();
}
async function generateConnectionString(env, label) {
  const { stdout } = await execute(process.execPath, ['--input-type=module', '-e',
    `import { pairConnectionString } from './src/configuration.mjs'; console.log(await pairConnectionString(${JSON.stringify(label)}));`],
    { cwd: root, env });
  return stdout.trim();
}

test('self-observation: host sees its own sessions with zero watchers, identity persists across restart, and coexists with a separately paired remote watcher', {
  skip: process.platform !== 'win32', timeout: 90000,
}, async t => {
  const dir = await mkdtemp(path.join(os.tmpdir(), 'monitor-self-observe-fixture-'));
  const hub = path.join(dir, 'hub'), home = path.join(dir, 'home');
  const uiPort = await freePort(), ingestPort = await freePort();
  const env = { ...process.env, MONITOR_DATA_DIR: hub, MONITOR_PORT: String(uiPort), USERPROFILE: home };
  const owned = [];
  let server, output = '';
  const spawnOwned = (args, extra = {}) => {
    const child = spawn(process.execPath, args, { cwd: root, env: { ...env, ...extra }, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] });
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

  await execute(process.execPath, [path.join(root, 'src', 'configuration.mjs'), 'initialize', '127.0.0.1', String(ingestPort)], { env });
  await emptyCopilotHome(home);

  const url = `http://127.0.0.1:${uiPort}`;
  const get = async () => (await fetch(`${url}/api/status`, { signal: AbortSignal.timeout(3000) })).json();
  async function waitFor(predicate, description, timeout = 15000) {
    const start = Date.now();
    while (Date.now() - start < timeout) {
      let result;
      try { result = await get(); } catch (error) { if (error.cause?.code !== 'ECONNREFUSED') throw error; }
      if (result && predicate(result)) return result;
      if (server) assert.equal(server.exitCode, null, output);
      await pause(150);
    }
    assert.fail(`${description}: ${output}`);
  }
  const start = () => {
    server = spawnOwned(['--disable-warning=ExperimentalWarning', path.join(root, 'src', 'server.mjs')]);
    return waitFor(state => state.healthy, 'Collector startup');
  };
  const localPost = async (route, body, headers = {}) => {
    const token = (await (await fetch(`${url}/api/control`)).json()).token;
    return fetch(`${url}${route}`, { method: 'POST', body: JSON.stringify(body),
      headers: { Authorization: `Bearer ${token}`, ...headers }, signal: AbortSignal.timeout(5000) });
  };

  await start();
  const initial = await waitFor(state => state.sources.length === 1 && state.sources[0].healthy,
    'Self-observer reports its own (empty) sessions with zero paired watchers');
  assert.equal(initial.sessions.length, 0);
  assert.equal(initial.active.length, 0);
  assert.ok(!initial.notification.kind, 'no completion notification should be conjured on fresh startup');

  const configFile = path.join(hub, 'collector.json');
  const config1 = JSON.parse(await readFile(configFile, 'utf8'));
  assert.equal(config1.reporters.length, 1, 'self-observation must not create more than one reporter identity');
  assert.equal(config1.reporters[0].legacy, true);
  const localReporterId = config1.reporters[0].id;
  assert.equal(initial.sources[0].id, localReporterId);

  const identityFile = path.join(hub, 'collector-local-identity.json');
  const identity1 = JSON.parse(await readFile(identityFile, 'utf8'));
  assert.equal(identity1.generation, 1);

  // Stop and restart: identity persists (installationId stable, generation increments),
  // and restart itself must never replay/invent a completion notification.
  assert.equal((await localPost('/api/stop')).status, 200);
  await server.finished;
  await start();
  const afterStop = await waitFor(state => state.sources.length === 1 && state.sources[0].healthy,
    'Self-observer reconnects and re-reports after restart');
  assert.ok(!afterStop.notification.kind, 'restart must not surface a spurious completion notification');
  const identity2 = JSON.parse(await readFile(identityFile, 'utf8'));
  assert.equal(identity2.installationId, identity1.installationId);
  assert.equal(identity2.generation, identity1.generation + 1);
  assert.notEqual(identity2.bootId, identity1.bootId);

  // Pair and connect a genuinely separate remote watcher while the self-observer keeps
  // running; the two sources must coexist, each independently healthy, with family
  // grouping/notification/dismissal working normally for the remote machine.
  const connectionString = await generateConnectionString(env, 'Remote PC fixture');
  const pairing = decodeConnectionString(connectionString);
  const remote = new Reporter(pairing, { installationId: randomUUID(), bootId: randomUUID(), generation: 1 });
  await remote.connect();
  await remote.send({ healthy: true, issues: [], relatives: [], notices: [], members: [rawMember('working')] });

  const working = await waitFor(state => state.sources.length === 2 && state.active.length === 1 &&
    state.sources.find(source => source.id === localReporterId)?.healthy,
  'Remote watcher reporting while the self-observer stays healthy and untouched');
  const remoteSource = working.sources.find(source => source.id === pairing.reporterId);
  assert.equal(remoteSource.healthy, true);
  assert.equal(working.active[0].reporterId, pairing.reporterId);

  const key = digest('synthetic-self-observation-finish');
  await remote.send({ healthy: true, issues: [], relatives: [],
    notices: [{ kind: 'finished', familyId: 'parent', key }], members: [rawMember('finished')] });
  const finished = await waitFor(state => state.notification.kind === 'finished', 'Remote family finished notice');
  assert.equal(finished.active.length, 0);
  const row = finished.sessions.find(row => row.reporterId === pairing.reporterId);
  assert.equal(row.state, 'finished');
  const dismissResult = await (await localPost('/api/dismiss', { entries: [{ id: row.id, key: row.dismissKey }] })).json();
  assert.equal(dismissResult.dismissed.length, 1);
  const afterDismiss = await get();
  assert.ok(!afterDismiss.sessions.some(entry => entry.id === row.id));
  assert.equal(afterDismiss.sources.find(source => source.id === localReporterId).healthy, true,
    'self-observer must remain healthy and unaffected by the remote watcher dismissal');

  await remote.disconnect();
});

test('migrating an existing single-machine install: self-observer claims the existing legacy reporter without duplicating it, backs up old state, and surfaces a migrated session as unconfirmed rather than duplicated or dropped', {
  skip: process.platform !== 'win32', timeout: 60000,
}, async t => {
  const dir = await mkdtemp(path.join(os.tmpdir(), 'monitor-self-observe-migrate-fixture-'));
  const hub = path.join(dir, 'hub'), home = path.join(dir, 'home');
  const uiPort = await freePort(), ingestPort = await freePort();
  const env = { ...process.env, MONITOR_DATA_DIR: hub, MONITOR_PORT: String(uiPort), USERPROFILE: home };
  const owned = [];
  let server, output = '';
  const spawnOwned = (args, extra = {}) => {
    const child = spawn(process.execPath, args, { cwd: root, env: { ...env, ...extra }, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] });
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

  await execute(process.execPath, [path.join(root, 'src', 'configuration.mjs'), 'initialize', '127.0.0.1', String(ingestPort)], { env });
  await emptyCopilotHome(home);

  // Seed a pre-existing SINGLE-MACHINE install: one legacy reporter, a finished session
  // row in the old sessions.json format, a dismissal + notification ledger, and a prior
  // watcher-identity.json (as a previously separately-run watcher.mjs for this same
  // machine would have left behind).
  const configFile = path.join(hub, 'collector.json');
  const config = JSON.parse(await readFile(configFile, 'utf8'));
  const legacyReporterId = randomUUID();
  config.reporters = [{ id: legacyReporterId, label: os.hostname(), tokenHash: digest('x'.repeat(64)), legacy: true }];
  await writeFile(configFile, JSON.stringify(config));
  const old = { ...rawMember('finished'), machine: 'OLD-SYNTHETIC' };
  const oldKey = groupFamilies([old])[0].dismissKey;
  await writeFile(path.join(hub, 'sessions.json'), JSON.stringify({ version: 3,
    sessions: [old], dismissed: [{ id: old.id, key: oldKey }] }));
  const originalKeys = ['c'.repeat(64)];
  await writeFile(path.join(hub, 'notifications.json'), JSON.stringify({ version: 1, keys: originalKeys }));
  const priorIdentity = { installationId: randomUUID(), generation: 5 };
  await writeFile(path.join(hub, 'watcher-identity.json'), JSON.stringify(priorIdentity));

  const url = `http://127.0.0.1:${uiPort}`;
  const get = async () => (await fetch(`${url}/api/status`, { signal: AbortSignal.timeout(3000) })).json();
  async function waitFor(predicate, description, timeout = 15000) {
    const start = Date.now();
    while (Date.now() - start < timeout) {
      let result;
      try { result = await get(); } catch (error) { if (error.cause?.code !== 'ECONNREFUSED') throw error; }
      if (result && predicate(result)) return result;
      if (server) assert.equal(server.exitCode, null, output);
      await pause(150);
    }
    assert.fail(`${description}: ${output}`);
  }

  server = spawnOwned(['--disable-warning=ExperimentalWarning', path.join(root, 'src', 'server.mjs')]);
  await waitFor(state => state.healthy, 'Migrated collector startup');

  const configAfter = JSON.parse(await readFile(configFile, 'utf8'));
  assert.equal(configAfter.reporters.length, 1, 'migration must not create a duplicate reporter identity');
  assert.equal(configAfter.reporters[0].id, legacyReporterId);

  assert.ok((await readdir(hub)).some(name => name.startsWith('backup-')), 'old single-machine state must be backed up, not discarded');
  assert.deepEqual(JSON.parse(await readFile(path.join(hub, 'notifications.json'), 'utf8')).keys, originalKeys);

  const identity = JSON.parse(await readFile(path.join(hub, 'collector-local-identity.json'), 'utf8'));
  assert.equal(identity.installationId, priorIdentity.installationId, 'prior watcher identity must be carried over, not replaced');
  assert.equal(identity.generation, priorIdentity.generation + 1);

  // Once the self-observer completes its first real (synthetic-empty) poll, the
  // migrated 'finished' row must surface as unconfirmed - never silently dropped,
  // never duplicated into a second reporter.
  const afterPoll = await waitFor(state => state.sources.length === 1 && state.sources[0].healthy,
    'Self-observer completes its first real poll after migration');
  const migratedRow = afterPoll.sessions.find(row => row.id.endsWith(`~${old.id}`));
  assert.ok(migratedRow, 'migrated legacy session must still be present, not dropped');
  assert.equal(migratedRow.state, 'unknown');
  assert.match(migratedRow.detail, /omitted|unconfirmed/i);
});
