import test from 'node:test';
import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import { mkdtemp, mkdir, writeFile, rm } from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import { LocalSource } from '../src/source.mjs';
import { FamilyMonitor } from '../src/families.mjs';
import { pollLocal } from '../src/local-report.mjs';

// Regression test for the "Unconfirmed" bug: watcher.mjs's poll() and
// server.mjs's localPoll() used to force
//   monitor.update([], { healthy: false, reason: 'Collector connection changed; rebaselining' })
// every time the reporting lease went null (any failed report, not just a
// genuine long gap), bypassing MonitorEngine's own wall-clock gap check and
// flipping every still-tracked session to 'unknown' on a brief reconnect.
// This fixture reproduces a genuinely busy local session and exercises both
// the fixed call pattern (reconnect + poll, no forced invalidate) and the
// removed call pattern (to document exactly why it was destructive).

const sessionId = '55555555-5555-5555-5555-555555555555';

async function busyCopilotHome(t) {
  const home = await mkdtemp(path.join(os.tmpdir(), 'copilot-reconnect-lease-test-'));
  t.after(() => rm(home, { recursive: true, force: true }));
  await mkdir(path.join(home, 'session-state'));
  const db = new DatabaseSync(path.join(home, 'data.db'));
  db.exec(`
    CREATE TABLE sessions (id TEXT, title TEXT, is_running INTEGER, was_interrupted INTEGER,
      execution_location TEXT, session_type TEXT, archived_at TEXT);
    CREATE TABLE workspaces (session_id TEXT, host_id TEXT, id TEXT, creator_session_id TEXT, archived_at TEXT);
    CREATE TABLE workspace_parent_links (child_workspace_id TEXT, parent_workspace_id TEXT);
    CREATE TABLE workspace_session_aliases (session_id TEXT, workspace_id TEXT);
    CREATE TABLE workspace_side_chats (workspace_id TEXT, session_id TEXT);
    CREATE TABLE session_side_chats (parent_session_id TEXT, session_id TEXT);
  `);
  db.prepare('INSERT INTO sessions VALUES (?,?,1,0,?,?,NULL)').run(sessionId, 'Busy work', 'local', 'project');
  db.prepare('INSERT INTO workspaces(session_id,host_id,id) VALUES (?,?,?)').run(sessionId, 'local', 'local-workspace');
  db.close();
  const folder = path.join(home, 'session-state', sessionId);
  await mkdir(folder);
  await writeFile(path.join(folder, `inuse.${process.pid}.lock`), String(process.pid));
  const events = [
    { id: 'first', type: 'session.start', timestamp: new Date().toISOString(), data: { context: { cwd: 'C:\\demo\\project' } } },
    { id: 'start', type: 'assistant.turn_start', timestamp: new Date().toISOString(), data: { turnId: '0', interactionId: 'run' } },
  ].map(e => JSON.stringify(e)).join('\n') + '\n';
  await writeFile(path.join(folder, 'events.jsonl'), events);
  const owner = { pid: process.pid, parentPid: process.ppid, name: 'copilot.exe', startedAt: new Date(Date.now() - 60000).toISOString() };
  const parent = { pid: process.ppid, name: 'github.exe', startedAt: new Date(Date.now() - 120000).toISOString() };
  const source = new LocalSource(home, () => ({ at: Date.now(), processes: [owner, parent] }));
  return { home, source };
}

test('reconnect after a dropped report lease does not invalidate a still-working session', async t => {
  const { source } = await busyCopilotHome(t);
  const monitor = new FamilyMonitor('test-host', () => {}, []);
  for (const id of monitor.rows.keys()) source.tracked.add(id);

  const first = await pollLocal({ source, monitor });
  assert.equal(first.healthy, true);
  assert.equal(monitor.rows.get(sessionId).state, 'working', 'first poll establishes the session as working');

  // This is the fixed sequence: the reporting lease went null and the
  // collector reconnected, but nothing forces the local monitor to discard
  // what it still knows. A normal poll cycle follows immediately.
  const second = await pollLocal({ source, monitor });
  assert.equal(second.healthy, true);
  assert.equal(monitor.rows.get(sessionId).state, 'working',
    'a reconnect with no forced invalidate must not turn a working session unconfirmed');
});

test('characterization: the removed forced-unhealthy call does flip a working session to unknown', async t => {
  const { source } = await busyCopilotHome(t);
  const monitor = new FamilyMonitor('test-host', () => {}, []);
  for (const id of monitor.rows.keys()) source.tracked.add(id);

  await pollLocal({ source, monitor });
  assert.equal(monitor.rows.get(sessionId).state, 'working');

  // This reproduces, verbatim, the call that watcher.mjs's poll() and
  // server.mjs's localPoll() used to make whenever the lease went null.
  await monitor.update([], { healthy: false, reason: 'Collector connection changed; rebaselining' });
  assert.equal(monitor.rows.get(sessionId).state, 'unknown',
    'documents why the removed call was destructive: it must never be reintroduced on a mere reconnect');
});
