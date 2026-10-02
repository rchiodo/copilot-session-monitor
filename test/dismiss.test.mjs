import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm, writeFile } from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import { Readable } from 'node:stream';
import { FamilyMonitor, groupFamilies } from '../src/families.mjs';
import { SessionStore, Ledger } from '../src/engine.mjs';
import { EventState } from '../src/events.mjs';
import { MonitorActions, readDismissEntries } from '../src/actions.mjs';

const at = '2026-10-02T20:00:00.000Z';
const row = (id, state = 'finished', parentId = null) => ({
  id, title: `Title ${id}`, machine: 'TEST', source: 'Copilot desktop', state, parentId,
  detail: 'Fixture status', activity: 'Agent running', runId: `old-${id}`,
  firstObservedAt: at, startedAt: at, lastEventAt: at, lastResponseAt: at,
  finishedAt: state === 'finished' ? at : null, contextOnly: false, hierarchyIssue: null, lastAlert: null,
});
const entries = monitor => groupFamilies([...monitor.rows.values()])
  .map(item => ({ id: item.id, key: item.dismissKey ?? 'a'.repeat(64) }));
const sample = (id, events, parentId = null, busy = true) => ({
  id, title: `Title ${id}`, source: 'Copilot desktop', events: events.snapshot(),
  alive: true, owner: 'owner', parentId, busy,
});
function event(events, type, id, data = {}) {
  events.accept({ type, id, timestamp: at, data });
}

test('individual/bulk dismissal only hides finished families, preserving observations and other states', () => {
  const rows = [
    row('p'), row('c', 'finished', 'p'), row('another'),
    row('work', 'working'), row('wait', 'waiting'), row('err', 'error'), row('unknown', 'unknown'),
  ];
  const monitor = new FamilyMonitor('TEST', async () => {});
  monitor.engine.rows = new Map(rows.map(item => [item.id, item]));
  const before = [...monitor.rows.entries()];
  const request = entries(monitor);
  assert.deepEqual(monitor.dismiss(request.slice(0, 1)).dismissed, [request[0].id]);
  const result = monitor.dismiss(request);
  assert.equal(result.dismissed.length, 2);
  assert.equal(result.skipped.length, 4);
  assert.equal(monitor.snapshot().sessions.length, 4);
  assert.deepEqual([...monitor.rows.entries()], before);
});

test('dismissed-run markers survive storage and restart without replay or removing dedupe', async t => {
  const dir = await mkdtemp(path.join(os.tmpdir(), 'monitor-dismiss-test-'));
  t.after(() => rm(dir, { recursive: true, force: true }));
  const ledger = new Ledger(path.join(dir, 'ledger.json'));
  await ledger.claim('existing-notification');
  const store = new SessionStore(path.join(dir, 'sessions.json'));
  const monitor = new FamilyMonitor('TEST', async key => ledger.claim(key), [row('p'), row('c', 'finished', 'p')]);
  monitor.dismiss(entries(monitor));
  await store.save(monitor.snapshot().members, monitor.dismissed);
  const loaded = new SessionStore(store.file);
  const restored = new FamilyMonitor('TEST', async () => assert.fail('Historical alert replayed'), await loaded.load(), loaded.dismissed);
  assert.equal(restored.snapshot().sessions.length, 0);
  await restored.update([], { now: 1000 });
  assert.equal(restored.snapshot().sessions.length, 0);
  assert.equal(restored.rows.size, 2);
  assert.equal(ledger.keys.size, 1);
  const malformed = { version: 3, sessions: [row('p')], dismissed: [{ id: 'p', key: 'invalid' }] };
  await writeFile(store.file, JSON.stringify(malformed));
  await assert.rejects(new SessionStore(store.file).load(), /Invalid dismissed/);
});

for (const id of ['p', 'c', 'new-descendant']) {
  test(`new work by ${id} restores the dismissed family and notifies once on its new completion`, async () => {
    const alerts = [];
    const monitor = new FamilyMonitor('TEST', async (key, alert) => alerts.push(alert),
      [row('p'), row('c', 'finished', 'p')]);
    const old = entries(monitor);
    monitor.dismiss(old);
    const events = new EventState();
    event(events, 'assistant.turn_start', 'start', { interactionId: `new-${id}`, turnId: '0' });
    const parent = id === 'p' ? null : 'p';
    const working = await monitor.update([sample(id, events, parent)], { now: 1000 });
    assert.equal(working.sessions[0].state, 'working');
    assert.equal(monitor.dismissed.size, 0);
    event(events, 'assistant.message', 'answer', { phase: 'final_answer', turnId: '0', toolRequests: [] });
    event(events, 'assistant.turn_end', 'end', { turnId: '0' });
    const done = await monitor.update([sample(id, events, parent, false)], { now: 2000 });
    assert.equal(done.sessions[0].state, 'finished');
    assert.equal(monitor.dismiss(old).skipped.length, 1);
    await monitor.update([sample(id, events, parent, false)], { now: 3000 });
    assert.equal(alerts.filter(alert => alert.kind === 'finished').length, 1);
  });
}

test('fresh-state serialized guard skips a resumed family but dismisses unchanged bulk entries', async () => {
  const monitor = new FamilyMonitor('TEST', async () => {}, [row('p'), row('other')]);
  const request = entries(monitor);
  const events = new EventState();
  event(events, 'assistant.turn_start', 'start', { interactionId: 'resumed', turnId: '0' });
  let refreshed = false, persisted = false, release;
  const actions = new MonitorActions(monitor, async () => {
    refreshed = true;
    await monitor.update([sample('p', events)], { now: 1000 });
  }, async () => { persisted = true; }, () => true);
  const ongoing = actions.run(() => new Promise(resolve => { release = resolve; }));
  await new Promise(resolve => setImmediate(resolve));
  const dismissal = actions.dismiss(request);
  assert.equal(refreshed, false);
  release();
  await ongoing;
  const result = await dismissal;
  assert.deepEqual(result.dismissed, ['other']);
  assert.equal(result.skipped[0].id, 'p');
  assert.equal(persisted, true);
  assert.equal(monitor.snapshot().sessions[0].state, 'working');
});

test('failed refresh or persistence cannot silently dismiss entries; malformed input is rejected', async () => {
  const monitor = new FamilyMonitor('TEST', async () => {}, [row('p')]);
  const request = entries(monitor);
  const offline = new MonitorActions(monitor, async () => {}, async () => assert.fail(), () => false);
  await assert.rejects(offline.dismiss(request), { status: 503 });
  const failure = new MonitorActions(monitor, async () => {}, async () => { throw new Error('Disk failure'); }, () => true);
  await assert.rejects(failure.dismiss(request), /Disk failure/);
  assert.equal(monitor.dismissed.size, 0);
  const valid = new MonitorActions(monitor, async () => {}, async () => {}, () => true);
  await assert.rejects(valid.dismiss([]), { status: 400 });
  await assert.rejects(readDismissEntries(Readable.from([Buffer.from('{')])), { status: 400 });
  await assert.rejects(readDismissEntries(Readable.from([Buffer.alloc(1048577)])), { status: 413 });
  assert.deepEqual(await readDismissEntries(Readable.from([Buffer.from(JSON.stringify({ entries: request }))])), request);
});

test('dormant linked names and missing metadata appear as unobserved, nested and without changing completion', () => {
  const members = [row('p'), row('c', 'finished', 'p')];
  const metadata = [
    { id: 'dormant', parentId: 'c', title: 'Full dormant grandchild name', detail: 'Execution not observed' },
    { id: 'missing', parentId: 'dormant', title: 'Name unavailable (missing)', hierarchyIssue: 'Metadata unavailable', detail: 'Not observed' },
    { id: 'unrelated', parentId: null, title: 'Never import unrelated history' },
  ];
  const family = groupFamilies(members, metadata)[0];
  assert.equal(family.state, 'finished');
  assert.deepEqual(family.relatives.map(item => [item.id, item.depth]), [['p', 0], ['c', 1], ['dormant', 2], ['missing', 3]]);
  assert.equal(family.relatives[2].state, 'unobserved');
  assert.equal(family.dismissKey, groupFamilies(members)[0].dismissKey);
});

test('restart does not flash a dismissed child-only family while its idle ancestor is baselined', async () => {
  const retained = [{ ...row('p', 'idle'), contextOnly: true }, row('c', 'finished', 'p')];
  const monitor = new FamilyMonitor('TEST', async () => {}, retained);
  monitor.engine.rows = new Map(retained.map(item => [item.id, item]));
  monitor.dismiss(entries(monitor));
  const restored = new FamilyMonitor('TEST', async () => assert.fail('No historical alert'), retained, monitor.dismissed);
  assert.equal(restored.snapshot().sessions.length, 0);
  await restored.update([{ ...sample('p', new EventState(), null, false), contextOnly: true }], { now: 1000 });
  assert.equal(restored.snapshot().sessions.length, 0);
  await restored.update([{ ...sample('p', new EventState(), null, false), alive: false, contextOnly: true }], { now: 2000 });
  assert.equal(restored.snapshot().sessions[0].state, 'unknown');
});
