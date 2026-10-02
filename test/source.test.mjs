import test from 'node:test';
import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import { mkdtemp, mkdir, writeFile, appendFile, readFile, rm } from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import { LocalSource, desktopRows } from '../src/source.mjs';

const localId = '11111111-1111-1111-1111-111111111111';
const remoteId = '22222222-2222-2222-2222-222222222222';
const cloudId = '33333333-3333-3333-3333-333333333333';
const cliId = '44444444-4444-4444-4444-444444444444';
async function fixture(t) {
  const home = await mkdtemp(path.join(os.tmpdir(), 'copilot-source-test-'));
  t.after(() => rm(home, { recursive: true, force: true }));
  await mkdir(path.join(home, 'session-state'));
  const file = path.join(home, 'data.db');
  const db = new DatabaseSync(file);
  db.exec(`
    CREATE TABLE sessions (id TEXT, title TEXT, is_running INTEGER, was_interrupted INTEGER,
      execution_location TEXT, session_type TEXT, archived_at TEXT);
    CREATE TABLE workspaces (session_id TEXT, host_id TEXT, id TEXT, creator_session_id TEXT, archived_at TEXT);
    CREATE TABLE workspace_parent_links (child_workspace_id TEXT, parent_workspace_id TEXT);
    CREATE TABLE workspace_session_aliases (session_id TEXT, workspace_id TEXT);
    CREATE TABLE workspace_side_chats (workspace_id TEXT, session_id TEXT);
    CREATE TABLE session_side_chats (parent_session_id TEXT, session_id TEXT);
  `);
  const insert = db.prepare('INSERT INTO sessions VALUES (?,?,1,0,?,?,NULL)');
  insert.run(localId, 'Local work', 'local', 'project');
  insert.run(remoteId, 'Remote work', 'local', 'project');
  insert.run(cloudId, 'Cloud work', 'cloud', 'project');
  db.prepare('INSERT INTO workspaces(session_id,host_id,id) VALUES (?,?,?)').run(localId, 'local', 'local-workspace');
  db.prepare('INSERT INTO workspaces(session_id,host_id,id) VALUES (?,?,?)').run(remoteId, 'ssh-host', 'remote-workspace');
  db.close();
  const events = [
    { id: 'first', type: 'session.start', timestamp: new Date().toISOString(), data: { context: { cwd: 'C:\\demo\\project' } } },
    { id: 'start', type: 'assistant.turn_start', timestamp: new Date().toISOString(), data: { turnId: '0', interactionId: 'run' } },
  ].map(e => JSON.stringify(e)).join('\n') + '\n';
  for (const id of [localId, remoteId, cloudId, cliId]) {
    const folder = path.join(home, 'session-state', id);
    await mkdir(folder);
    await writeFile(path.join(folder, `inuse.${process.pid}.lock`), String(process.pid));
    await writeFile(path.join(folder, 'events.jsonl'), events);
  }
  const owner = {
    pid: process.pid, parentPid: process.ppid, name: 'copilot.exe', startedAt: new Date(Date.now() - 60000).toISOString(),
  };
  const parent = { pid: process.ppid, name: 'github.exe', startedAt: new Date(Date.now() - 120000).toISOString() };
  return { home, file, owner, parent };
}

async function finishTurn(home, id) {
  await appendFile(path.join(home, 'session-state', id, 'events.jsonl'), [
    { id: 'final', type: 'assistant.message', data: { turnId: '0', toolRequests: [] } },
    { id: 'end', type: 'assistant.turn_end', data: { turnId: '0' } },
  ].map(e => JSON.stringify({ ...e, timestamp: new Date().toISOString() })).join('\n') + '\n');
}

test('desktop adapter is read-only, excludes other hosts/cloud, and recognizes live owners', async t => {
  const { home, file, owner, parent } = await fixture(t);
  const before = await readFile(file);
  const source = new LocalSource(home, () => ({ at: Date.now(), processes: [owner, parent] }));
  const result = await source.poll();
  assert.equal(result.samples.length, 1);
  assert.equal(result.samples[0].id, localId);
  assert.equal(result.samples[0].alive, true);
  assert.equal(result.samples[0].events.activeTurn, true);
  assert.deepEqual(await readFile(file), before);
  assert.equal(desktopRows(file).length, 3);
});

test('dead or reused owners cannot prove activity', async t => {
  const { home, owner, parent } = await fixture(t);
  for (const processes of [[], [{ ...owner, startedAt: new Date(Date.now() + 60000).toISOString() }, parent],
    [owner, { ...parent, startedAt: new Date(Date.now() + 60000).toISOString() }]]) {
    const result = await new LocalSource(home, () => ({ at: Date.now(), processes })).poll();
    assert.equal(result.samples[0].alive, false);
    assert.equal(result.samples[0].events, null);
    assert.ok(result.issues.length);
  }
});

test('stale process evidence and schema failures do not look like an empty healthy monitor', async t => {
  const { home, file } = await fixture(t);
  await assert.rejects(new LocalSource(home, () => ({ at: Date.now() - 20000, processes: [] })).poll(), /unavailable/);
  const db = new DatabaseSync(file);
  db.exec('DROP TABLE workspaces');
  db.close();
  await assert.rejects(new LocalSource(home, () => ({ at: Date.now(), processes: [] })).poll());
});

test('CLI without a desktop parent uses metadata fallback and is activity-only', async t => {
  const { home, owner } = await fixture(t);
  const result = await new LocalSource(home, () => ({ at: Date.now(), processes: [owner] })).poll();
  const cli = result.samples.find(row => row.id === cliId);
  assert.equal(cli.source, 'CLI (activity only)');
  assert.equal(cli.title, `project - CLI ${cliId.slice(0, 8)}`);
  assert.equal(cli.busy, true);
  assert.equal(result.samples.some(row => row.id === remoteId || row.id === cloudId), false);
});

test('restored tracking reconciles only known idle sessions without importing the idle archive', async t => {
  const { home, file, owner, parent } = await fixture(t);
  await finishTurn(home, localId);
  const db = new DatabaseSync(file);
  db.exec('UPDATE sessions SET is_running=0');
  db.close();
  const source = new LocalSource(home, () => ({ at: Date.now(), processes: [owner, parent] }));
  assert.equal((await source.poll()).samples.length, 0);
  source.tracked.add(localId);
  const result = await source.poll();
  assert.deepEqual(result.samples.map(row => row.id), [localId]);
  assert.equal(result.samples[0].busy, false);
  source.releaseIdle(new Set());
  assert.equal((await source.poll()).samples.length, 0);
});

test('child-only activity reads its idle chat parent, not unrelated idle history', async t => {
  const { home, file, owner, parent } = await fixture(t);
  await finishTurn(home, cliId);
  const db = new DatabaseSync(file);
  db.prepare('INSERT INTO sessions VALUES (?,?,0,0,?,?,NULL)').run(cliId, 'Chat parent', 'local', 'general_chat');
  db.prepare('UPDATE workspaces SET creator_session_id=? WHERE session_id=?').run(cliId, localId);
  db.close();
  const source = new LocalSource(home, () => ({ at: Date.now(), processes: [owner, parent] }));
  const result = await source.poll();
  assert.deepEqual(result.samples.map(row => row.id).sort(), [localId, cliId]);
  const child = result.samples.find(row => row.id === localId);
  const chat = result.samples.find(row => row.id === cliId);
  assert.equal(child.parentId, cliId);
  assert.equal(chat.contextOnly, true);
  assert.equal(chat.alive, true);
  assert.equal(chat.busy, false);
});

test('missing parent link does not hide reliable local child execution', async t => {
  const { home, file, owner, parent } = await fixture(t);
  const db = new DatabaseSync(file);
  db.prepare('INSERT INTO workspace_parent_links VALUES (?,?)').run('local-workspace', 'missing-parent');
  db.close();
  const result = await new LocalSource(home, () => ({ at: Date.now(), processes: [owner, parent] })).poll();
  const child = result.samples.find(row => row.id === localId);
  assert.equal(child.alive, true);
  assert.equal(child.events.activeTurn, true);
  assert.equal(child.readError, undefined);
  assert.match(child.hierarchyIssue, /missing/);
  assert.match(result.samples.find(row => row.id === 'missing-parent').readError, /missing/);
  assert.ok(result.issues.length);
});

test('additional dormant nested children provide names only, without opening their event files', async t => {
  const { home, file, owner, parent } = await fixture(t);
  const db = new DatabaseSync(file);
  db.prepare('UPDATE sessions SET is_running=0 WHERE id!=?').run(localId);
  db.prepare('INSERT INTO workspace_parent_links VALUES (?,?)').run('remote-workspace', 'local-workspace');
  db.prepare('INSERT INTO workspaces(session_id,host_id,id) VALUES (?,?,?)').run(cloudId, 'remote-host', 'grand-workspace');
  db.prepare('INSERT INTO workspace_parent_links VALUES (?,?)').run('grand-workspace', 'remote-workspace');
  db.close();
  const source = new LocalSource(home, () => ({ at: Date.now(), processes: [owner, parent] }));
  const result = await source.poll();
  assert.deepEqual(result.samples.map(item => item.id), [localId]);
  assert.equal(result.relatives.find(item => item.id === remoteId).title, 'Remote work');
  assert.equal(result.relatives.find(item => item.id === cloudId).parentId, remoteId);
  assert.deepEqual([...source.tails.keys()], [localId]);
});

test('idle persisted flag discovers current root execution but never idle live processes', async t => {
  const { home, file, owner, parent } = await fixture(t);
  const db = new DatabaseSync(file);
  db.exec('UPDATE sessions SET is_running=0');
  db.close();
  const source = new LocalSource(home, () => ({ at: Date.now(), processes: [owner, parent] }));
  assert.equal((await source.poll()).samples.find(row => row.id === localId).busy, true);
  await finishTurn(home, localId);
  const fresh = new LocalSource(home, () => ({ at: Date.now(), processes: [owner, parent] }));
  assert.equal((await fresh.poll()).samples.length, 0);
});

test('attached background work with is_running=0 is discovered under its canonical idle parent', async t => {
  const { home, file, owner, parent } = await fixture(t);
  const db = new DatabaseSync(file);
  db.exec('UPDATE sessions SET is_running=0');
  db.prepare('INSERT INTO sessions VALUES (?,?,0,0,?,?,NULL)').run(cliId, 'Idle root', 'local', 'general_chat');
  db.prepare('UPDATE workspaces SET creator_session_id=? WHERE session_id=?').run(cliId, localId);
  db.close();
  await finishTurn(home, cliId);
  await appendFile(path.join(home, 'session-state', localId, 'events.jsonl'), [
    { id: 'launch', type: 'tool.execution_start', data: { toolName: 'powershell', toolCallId: 'shell', arguments: {} } },
    { id: 'running', type: 'tool.execution_complete', data: { toolCallId: 'shell', success: true, result: {
      content: '<command with shellId: shell-a is still running after 180 seconds. No output yet.>',
    } } },
  ].map(e => JSON.stringify({ ...e, timestamp: new Date().toISOString() })).join('\n') + '\n');
  await finishTurn(home, localId);
  const source = new LocalSource(home, () => ({ at: Date.now(), processes: [owner, parent] }));
  const result = await source.poll();
  assert.equal(result.samples.find(row => row.id === localId).busy, true);
  assert.equal(result.samples.find(row => row.id === localId).events.backgroundCount, 1);
  assert.equal(result.samples.find(row => row.id === localId).events.terminal, null);
  assert.equal(result.samples.find(row => row.id === cliId).busy, false);
  assert.equal(result.samples.find(row => row.id === cliId).contextOnly, true);
  source.releaseIdle(new Set([localId, cliId]));
  assert.equal((await source.poll()).samples.find(row => row.id === localId).busy, true);
});

test('historical open background work from a different owner lifetime is unconfirmed, never Working', async t => {
  const { home, file, owner, parent } = await fixture(t);
  const db = new DatabaseSync(file); db.exec('UPDATE sessions SET is_running=0'); db.close();
  const timestamp = new Date(Date.parse(owner.startedAt) - 10000).toISOString();
  await appendFile(path.join(home, 'session-state', localId, 'events.jsonl'), [
    { id: 'launch', type: 'tool.execution_start', data: { toolName: 'powershell', toolCallId: 'old', arguments: {} } },
    { id: 'pending', type: 'tool.execution_complete', data: { toolCallId: 'old', success: true,
      result: { content: '<command started in background with shellId: old>' } } },
  ].map(e => JSON.stringify({ ...e, timestamp })).join('\n') + '\n');
  await finishTurn(home, localId);
  const result = await new LocalSource(home, () => ({ at: Date.now(), processes: [owner, parent] })).poll();
  const row = result.samples.find(item => item.id === localId);
  assert.equal(row.busy, false);
  assert.match(row.activityUnconfirmed, /ownership/);
});
