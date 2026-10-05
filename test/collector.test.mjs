import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm, readFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { randomUUID } from 'node:crypto';
import { Collector } from '../src/collector.mjs';
import { metadata, observedMetadata, validateReport, digest, HEARTBEAT_MS } from '../src/protocol.mjs';
import { groupFamilies } from '../src/families.mjs';
import { Ledger } from '../src/engine.mjs';

const at = '2026-01-01T12:00:00.000Z';
const now = Date.parse(at);
const member = (state = 'working', id = 'parent', parentId = null) => ({
  id, parentId, title: `Synthetic ${id}`, machine: 'SYNTHETIC', source: 'Copilot desktop', state,
  detail: 'Synthetic state', activity: 'Executing tools', runId: `run-${id}`, firstObservedAt: at,
  startedAt: at, lastResponseAt: at, lastEventAt: at, finishedAt: state === 'finished' ? at : null,
  hierarchyIssue: null, contextOnly: false, lastAlert: null,
});
async function fixture(t, legacy = [], dismissed = new Map()) {
  const dir = await mkdtemp(path.join(os.tmpdir(), 'monitor-collector-fixture-'));
  t.after(() => rm(dir, { recursive: true, force: true }));
  const config = { reporters: [1, 2].map((_, index) => ({ id: randomUUID(), label: 'Duplicate hostname',
    tokenHash: 'a'.repeat(64), legacy: index === 0 })) };
  const alerts = [], file = path.join(dir, 'collector-state.json');
  const ledger = new Ledger(path.join(dir, 'notifications.json'));
  await ledger.load();
  const notify = async (key, alert) => { if (await ledger.claimDigest(key)) alerts.push(alert); };
  const collector = new Collector(file, notify);
  await collector.load(config, legacy, dismissed);
  const identities = config.reporters.map(pair => ({ version: 1, reporterId: pair.id,
    installationId: randomUUID(), bootId: randomUUID(), generation: 1 }));
  const report = async (index, rows, options = {}) => {
    const source = collector.sources.get(identities[index].reporterId);
    const value = { version: 1, reporterId: source.id, lease: source.lease, seq: source.seq + 1,
      sentAt: new Date(options.now ?? now).toISOString(), healthy: true, issues: [], notices: [],
      ...metadata({ members: rows }), ...options };
    delete value.now;
    await collector.accept(value, options.now ?? now);
    return value;
  };
  const connect = (index = 0, time = now) => collector.connect(identities[index], time);
  return { collector, connect, report, identities, alerts, file, config, notify, ledger };
}
const finishNotice = (id = 'parent') => ({ key: digest(`finished-${id}`), familyId: id, kind: 'finished' });

test('reporters namespace colliding sessions/hostnames and aggregate descendants without cross-machine links', async t => {
  const f = await fixture(t);
  await f.connect(0); await f.connect(1);
  await f.report(0, [member('finished'), member('working', 'child', 'parent'), member('working', 'nested', 'child')]);
  await f.report(1, [member('finished')]);
  const view = f.collector.snapshot(now);
  assert.equal(view.sessions.length, 2);
  assert.notEqual(view.sessions[0].id, view.sessions[1].id);
  assert.equal(view.active.length, 1);
  assert.equal(view.active[0].runningCount, 2);
  assert.equal(view.active[0].relatives.length, 3);
  assert.equal(f.alerts.length, 0, 'Baseline finished rows never alert');
});

test('explicit completion, retry dedupe, restart baselines, and durable dismissal', async t => {
  const f = await fixture(t);
  await f.connect();
  await f.report(0, [member()]);
  const report = await f.report(0, [member('finished')], { notices: [finishNotice()] });
  assert.equal(f.alerts.length, 1);
  assert.equal((await f.collector.accept(report, now)).duplicate, true);
  assert.equal(f.alerts.length, 1);
  const row = f.collector.snapshot(now).sessions[0];
  assert.equal(f.collector.dismiss([{ id: row.id, key: row.dismissKey }], now).dismissed.length, 1);
  await f.collector.save();
  assert.equal(f.collector.snapshot(now).sessions.length, 0);
  const restart = new Collector(f.file, f.notify);
  await restart.load(f.config);
  assert.equal(restart.snapshot(now).sessions[0].state, 'unknown', 'Disconnected finish is never authoritative');
  const lease = await restart.connect(f.identities[0], now);
  await restart.accept({ ...report, lease: lease.lease, seq: 1 }, now);
  assert.equal(restart.snapshot(now).sessions.length, 0);
  assert.equal(f.alerts.length, 1);
  assert.equal(JSON.parse(await readFile(f.file)).dismissed.length, 1);
});

test('omissions, unsupported/unknown members, clock skew, reader failures and heartbeat loss block completion', async t => {
  for (const mode of ['omission', 'unknown', 'reader', 'clock', 'heartbeat']) {
    const f = await fixture(t);
    await f.connect();
    await f.report(0, [member(), member('working', 'child', 'parent')]);
    if (mode === 'heartbeat') {
      const view = f.collector.snapshot(now + HEARTBEAT_MS);
      assert.equal(view.sessions[0].state, 'unknown');
      await assert.rejects(f.report(0, [member('finished')], { now: now + HEARTBEAT_MS }), /Lease expired/);
      await f.connect(0, now + HEARTBEAT_MS);
    }
    const rows = [member('finished')];
    if (mode !== 'omission') rows.push(member(mode === 'unknown' ? 'unknown' : 'finished', 'child', 'parent'));
    await f.report(0, rows, { healthy: mode !== 'reader',
      sentAt: mode === 'clock' ? new Date(now - 31000).toISOString() : at,
      notices: [finishNotice()], ...(mode === 'heartbeat' ? { now: now + HEARTBEAT_MS } : {}) });
    assert.equal(f.alerts.length, 0, mode);
    if (mode !== 'heartbeat') assert.equal(f.collector.snapshot(now).sessions[0].state, 'unknown', mode);
  }
});

test('a continuously healthy watcher narrowing its report window keeps a retained finished row trusted, but a fresh baseline still distrusts the same omission', async t => {
  const f = await fixture(t);
  await f.connect();
  await f.report(0, [member('working', 'done')]);
  await f.report(0, [member('finished', 'done')], { notices: [finishNotice('done')] });
  assert.equal(f.alerts.length, 1, 'continuously observed completion is confirmed and notified');

  await f.report(0, []);
  assert.equal(f.collector.sources.get(f.identities[0].reporterId).members.find(row => row.id === 'done').state,
    'finished', 'a healthy, already-baselined watcher narrowing its window keeps a retained finished row trusted');

  await f.connect(0, now + 1);
  await f.report(0, [], { now: now + 1 });
  assert.equal(f.collector.sources.get(f.identities[0].reporterId).members.find(row => row.id === 'done').state,
    'unknown', 'a fresh baseline (reconnect) cannot vouch for a retained row it did not just observe');
});

test('parent/descendant work restores dismissed families and stale dismiss revisions skip resumed state', async t => {
  for (const child of [false, true]) {
    const f = await fixture(t);
    await f.connect(); await f.report(0, [member()]);
    await f.report(0, [member('finished')], { notices: [finishNotice()] });
    const finished = f.collector.snapshot(now).sessions[0], entry = { id: finished.id, key: finished.dismissKey };
    f.collector.dismiss([entry], now);
    await f.report(0, child ? [member('finished'), member('working', 'new-child', 'parent')]
      : [{ ...member(), runId: 'new-run' }]);
    const active = f.collector.snapshot(now).active[0];
    assert.ok(active);
    assert.equal(active.dismissKey, null);
    assert.equal(f.collector.dismiss([entry], now).skipped.length, 1);
  }
});

test('leases fence duplicate installations, live competing boots, replay and reordered reports', async t => {
  const f = await fixture(t);
  await f.connect();
  const report = await f.report(0, [member()]);
  await assert.rejects(f.collector.accept({ ...report, seq: 4 }, now), /Out-of-order/);
  await assert.rejects(f.collector.accept({ ...report, members: [] }, now), /conflicting/);
  await assert.rejects(f.collector.connect({ ...f.identities[0], installationId: randomUUID() }, now), /identity conflict/);
  await assert.rejects(f.collector.connect({ ...f.identities[0], bootId: randomUUID(), generation: 2 }, now), /holds this identity/);
  await f.connect();
  await assert.rejects(f.collector.accept(report, now), /Lease expired/);
  const replacement = { ...f.identities[0], bootId: randomUUID(), generation: 2 };
  await f.collector.connect(replacement, now + HEARTBEAT_MS);
  await assert.rejects(f.collector.connect(f.identities[0], now + HEARTBEAT_MS), /Old watcher/);
});

test('legacy migration preserves first-seen, dismissed revision and notification digest', async t => {
  const row = member('finished'), family = groupFamilies([row])[0];
  const f = await fixture(t, [row], new Map([[row.id, family.dismissKey]]));
  await f.ledger.claim('original-notification');
  const before = new Set(f.ledger.keys);
  await f.connect();
  await f.report(0, [{ ...row, firstObservedAt: '2025-12-31T12:00:00.000Z' }], { notices: [finishNotice()] });
  assert.equal(f.collector.snapshot(now).sessions.length, 0);
  assert.equal(f.collector.snapshot(now).members[0].firstObservedAt, at);
  assert.deepEqual(f.ledger.keys, before);
  assert.equal(f.alerts.length, 0);
});

test('protocol rejects unsupported versions, unbounded or extra payloads and false parent alerts', () => {
  const good = { version: 1, reporterId: randomUUID(), lease: 'a'.repeat(64), seq: 1,
    sentAt: at, healthy: true, issues: [], notices: [], ...metadata({ members: [member()] }) };
  assert.equal(validateReport(good), good);
  for (const bad of [{ ...good, version: 2 }, { ...good, prompt: 'Not metadata' },
    { ...good, members: [...good.members, ...good.members] },
    { ...good, members: [{ ...good.members[0], title: 'x'.repeat(513) }] },
    { ...good, members: [{ ...good.members[0], lastAlert: { sessionId: 'child', key: 'x', kind: 'finished', message: 'x', at } }] }]) {
    assert.throws(() => validateReport(bad));
  }
});

test('reconnect cannot upgrade work completed during a gap, even on repeated fresh snapshots', async t => {
  const f = await fixture(t);
  await f.connect(); await f.report(0, [member()]);
  await f.connect();
  for (let i = 0; i < 3; i++) {
    await f.report(0, [member('finished')], { notices: [finishNotice()] });
    assert.equal(f.collector.snapshot(now).sessions[0].state, 'unknown');
  }
  assert.equal(f.alerts.length, 0);
  await f.report(0, [{ ...member(), runId: 'new-run' }]);
  await f.report(0, [{ ...member('finished'), runId: 'new-run' }], { notices: [finishNotice()] });
  assert.equal(f.alerts.length, 1);
});

test('continuously tracked late final flush can finish, but lost authority cannot', async t => {
  for (const completionTracked of [true, false]) {
    const f = await fixture(t);
    await f.connect(); await f.report(0, [member()]);
    await f.report(0, [{ ...member('unknown'), completionTracked }]);
    await f.report(0, [member('finished')], { notices: [finishNotice()] });
    assert.equal(f.alerts.length, completionTracked ? 1 : 0);
  }
});

test('watcher never exports stale finished authority for missing/dead/replaced owners', () => {
  const snapshot = { members: [member('finished')] };
  for (const sample of [null, { alive: false }, { alive: true, readError: 'Unavailable' },
    { alive: true, completionUnconfirmed: 'Owner changed', events: { terminal: {} } },
    { alive: true, events: { terminal: {}, replaced: true } }]) {
    const report = observedMetadata(snapshot, sample ? [{ id: 'parent', ...sample }] : []);
    assert.equal(report.members[0].state, 'unknown');
    assert.equal(report.members[0].finishedAt, null);
  }
  assert.equal(observedMetadata(snapshot, [{ id: 'parent', alive: true, events: { terminal: {} } }])
    .members[0].state, 'finished');
});

test('central parent alert remains parent-only and survives watcher cache loss', async t => {
  const f = await fixture(t);
  await f.connect();
  const alert = { sessionId: 'parent', key: 'parent-alert', kind: 'waiting', message: 'Input needed', at };
  await f.report(0, [{ ...member(), lastAlert: alert }, { ...member('working', 'child', 'parent'),
    lastAlert: { ...alert, sessionId: 'child', key: 'child-error', kind: 'error' } }]);
  await f.report(0, [member(), member('working', 'child', 'parent')]);
  const row = f.collector.snapshot(now).sessions[0];
  assert.equal(row.parentAlert.kind, 'waiting');
  assert.equal(row.parentAlert.sessionId, row.id);
});

test('failed report persistence rolls back sequence and state so the same report can safely retry', async t => {
  const f = await fixture(t);
  await f.connect(); await f.report(0, [member()]);
  const source = f.collector.sources.get(f.identities[0].reporterId);
  const report = { version: 1, reporterId: source.id, lease: source.lease, seq: source.seq + 1,
    sentAt: at, healthy: true, issues: [], ...metadata({ members: [member('finished')] }), notices: [finishNotice()] };
  f.collector.file = path.join(f.file, 'not-a-directory');
  await assert.rejects(f.collector.accept(report, now));
  assert.equal(f.collector.snapshot(now).sessions[0].state, 'working');
  assert.equal(f.alerts.length, 0);
  f.collector.file = f.file;
  await f.collector.accept(report, now);
  assert.equal(f.alerts.length, 1);
});
