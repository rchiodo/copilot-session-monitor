import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, writeFile, appendFile, rename, rm, readFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { EventState, JsonlTail } from '../src/events.mjs';
import { Ledger, MonitorEngine, SessionStore } from '../src/engine.mjs';

let serial = 0;
const event = (type, data = {}, extra = {}) => ({
  id: `event-${++serial}`, type, data, timestamp: new Date(1790000000000 + serial).toISOString(), ...extra,
});
const start = (interactionId = 'run-a', turnId = '0') => event('assistant.turn_start', { interactionId, turnId });
const message = (tools = [], phase = 'final_answer', turnId = '0') =>
  event('assistant.message', { turnId, phase, toolRequests: tools, content: 'PRIVATE TRANSCRIPT SENTINEL' });
const end = (turnId = '0') => event('assistant.turn_end', { turnId });
function state(...events) {
  const result = new EventState();
  for (const e of events) result.accept(e);
  return result;
}
function sample(s, overrides = {}) {
  return {
    id: 'session-a', title: 'Readable title', source: 'Copilot desktop', busy: true,
    interrupted: false, alive: true, owner: 'pid:createdAt', events: s.snapshot(), ...overrides,
  };
}
function rig(retained = []) {
  const alerts = [];
  const seen = new Set();
  const engine = new MonitorEngine('TEST-MACHINE', async (key, alert) => {
    if (!seen.has(key)) { alerts.push(alert); seen.add(key); }
  }, retained);
  return { engine, alerts };
}
async function temp(t) {
  const dir = await mkdtemp(path.join(os.tmpdir(), 'copilot-monitor-test-'));
  t.after(() => rm(dir, { recursive: true, force: true }));
  return dir;
}

test('historical completed sessions do not notify; observed live run finishes exactly once', async () => {
  const { engine, alerts } = rig();
  const old = state(start(), message(), end());
  assert.equal((await engine.update([sample(old, { busy: false })], { now: 1000 })).active.length, 0);
  assert.equal(alerts.length, 0);
  const current = state(start('run-b'));
  const active = await engine.update([sample(current)], { now: 2000 });
  assert.equal(active.active.length, 1);
  assert.equal(active.active[0].machine, 'TEST-MACHINE');
  current.accept(message()); current.accept(end());
  const done = await engine.update([sample(current, { busy: false })], { now: 3000 });
  assert.equal(done.active.length, 0);
  assert.equal(done.sessions.length, 1);
  assert.equal(done.sessions[0].state, 'finished');
  assert.equal(done.sessions[0].finishedAt, new Date(3000).toISOString());
  assert.equal(done.sessions[0].lastResponseAt, current.lastResponseAt);
  assert.equal(alerts.length, 1);
  assert.equal(alerts[0].kind, 'finished');
  assert.match(alerts[0].message, /does not mean/);
  await engine.update([sample(current, { busy: false })], { now: 4000 });
  assert.equal(alerts.length, 1);
  assert.equal(engine.snapshot().sessions[0].finishedAt, new Date(3000).toISOString());
});

test('response time comes only from root assistant messages and survives new runs and replay', () => {
  const s = state(start());
  assert.equal(s.snapshot().lastResponseAt, null);
  const first = message([], 'commentary');
  s.accept(first);
  s.accept(event('tool.execution_complete', { toolCallId: 't' }));
  s.accept(event('assistant.message', {}, { agentId: 'nested' }));
  s.accept(event('assistant.message', { parentToolCallId: 'nested' }));
  s.accept(event('session.resume'));
  s.accept(start('new-run'));
  s.accept(event('assistant.message', {}, { timestamp: new Date(Date.parse(first.timestamp) - 100).toISOString() }));
  assert.equal(s.snapshot().lastResponseAt, first.timestamp);
  const latest = message();
  s.accept(latest);
  assert.equal(s.snapshot().lastResponseAt, latest.timestamp);
  assert.equal(JSON.stringify(s.snapshot()).includes('PRIVATE TRANSCRIPT'), false);
});

test('retained rows sort by response, not working-first, completion time, tool time or polling time', async () => {
  const { engine } = rig();
  const olderResponse = message();
  const newerResponse = message();
  const older = state(start('older'), olderResponse);
  const newer = state(start('newer'), newerResponse);
  await engine.update([sample(older, { id: 'older' }), sample(newer, { id: 'newer' })], { now: 1000 });
  newer.accept(end());
  older.accept(event('tool.execution_start', { toolCallId: 't', toolName: 'powershell' }));
  let view = await engine.update([sample(older, { id: 'older' }), sample(newer, { id: 'newer', busy: false })], { now: 2000 });
  assert.deepEqual(view.sessions.map(row => [row.id, row.state]), [['newer', 'finished'], ['older', 'working']]);
  const finishedAt = view.sessions[0].finishedAt;
  older.accept(event('tool.execution_complete', { toolCallId: 't' }));
  view = await engine.update([sample(older, { id: 'older' })], { now: 3000 });
  assert.deepEqual(view.sessions.map(row => row.id), ['newer', 'older']);
  assert.equal(view.sessions[0].finishedAt, finishedAt);
  older.accept(message([], 'commentary'));
  view = await engine.update([sample(older, { id: 'older' })], { now: 4000 });
  assert.deepEqual(view.sessions.map(row => row.id), ['older', 'newer']);
});

test('missing response uses stable first observation, never invented response or repeated poll time', async () => {
  const { engine } = rig();
  const first = state(start('first')), second = state(start('second'));
  await engine.update([sample(first, { id: 'first' })], { now: 1000 });
  await engine.update([sample(first, { id: 'first' }), sample(second, { id: 'second' })], { now: 2000 });
  const view = await engine.update([sample(first, { id: 'first' }), sample(second, { id: 'second' })], { now: 3000 });
  assert.deepEqual(view.sessions.map(row => row.id), ['second', 'first']);
  assert.equal(view.sessions[1].lastResponseAt, null);
  assert.equal(view.sessions[1].firstObservedAt, new Date(1000).toISOString());
});

test('new and resumed runs replace one retained row without duplicate completion alerts', async () => {
  const { engine, alerts } = rig();
  const s = state(start());
  await engine.update([sample(s)], { now: 1000 });
  s.accept(message()); s.accept(end());
  await engine.update([sample(s, { busy: false })], { now: 2000 });
  const oldResponse = s.lastResponseAt;
  for (const [index, resume] of [false, true].entries()) {
    if (resume) s.accept(event('session.resume'));
    s.accept(start(`next-${index}`));
    let view = await engine.update([sample(s)], { now: 3000 + index * 2000 });
    assert.equal(view.sessions.length, 1);
    assert.equal(view.sessions[0].state, 'working');
    assert.equal(view.sessions[0].finishedAt, null);
    if (!resume) assert.equal(view.sessions[0].lastResponseAt, oldResponse);
    s.accept(message()); s.accept(end());
    view = await engine.update([sample(s, { busy: false })], { now: 4000 + index * 2000 });
    assert.equal(view.sessions.length, 1);
    assert.equal(view.sessions[0].state, 'finished');
    assert.equal(view.sessions[0].firstObservedAt, new Date(1000).toISOString());
  }
  assert.equal(alerts.filter(alert => alert.kind === 'finished').length, 3);
});

test('finished rows persist through outages and indefinitely without re-importing old idle sessions', async () => {
  const { engine, alerts } = rig();
  const s = state(start());
  await engine.update([sample(s)], { now: 1000 });
  s.accept(message()); s.accept(end());
  const finished = (await engine.update([sample(s, { busy: false })], { now: 2000 })).sessions[0];
  await engine.update([], { healthy: false, now: 3000 });
  await engine.update([], { now: 86400000 });
  const old = state(start('unobserved'), message(), end());
  const view = await engine.update([sample(old, { id: 'unobserved', busy: false })], { now: 86401000 });
  assert.deepEqual(view.sessions, [finished]);
  assert.equal(alerts.length, 1);
});

test('metadata-only store retains completed rows and restart never promotes downtime to completion', async t => {
  const store = new SessionStore(path.join(await temp(t), 'sessions.json'));
  assert.deepEqual(await store.load(), []);
  const { engine } = rig();
  const complete = state(start('complete')), running = state(start('running'));
  await engine.update([sample(complete), sample(running, { id: 'running' })], { now: 1000 });
  complete.accept(message()); complete.accept(end());
  const view = await engine.update([sample(complete, { busy: false }), sample(running, { id: 'running' })], { now: 2000 });
  await store.save(view.sessions.map(row => ({ ...row, transcript: 'PRIVATE TRANSCRIPT SENTINEL' })));
  const disk = await readFile(store.file, 'utf8');
  assert.equal(disk.includes('PRIVATE'), false);
  const restored = await new SessionStore(store.file).load();
  const restarted = rig(restored);
  assert.equal(restarted.engine.rows.get('running').state, 'unknown');
  assert.equal(restarted.engine.rows.get('session-a').state, 'finished');
  running.accept(message()); running.accept(end());
  const after = await restarted.engine.update([
    sample(complete, { busy: false }), sample(running, { id: 'running', busy: false }),
  ], { now: 10000 });
  assert.equal(after.sessions.length, 2);
  assert.equal(restarted.engine.rows.get('running').state, 'unknown');
  assert.equal(restarted.engine.rows.get('session-a').finishedAt, new Date(2000).toISOString());
  assert.equal(restarted.alerts.length, 0);
  running.accept(start('fresh'));
  await restarted.engine.update([sample(running, { id: 'running' })], { now: 11000 });
  running.accept(message()); running.accept(end());
  await restarted.engine.update([sample(running, { id: 'running', busy: false })], { now: 12000 });
  assert.equal(restarted.alerts.length, 1);
});

test('saved waiting state is unconfirmed after restart unless fresh running evidence supports it', async () => {
  const { engine } = rig();
  const s = state(start());
  await engine.update([sample(s)], { now: 1000 });
  s.accept(event('user_input.requested', { requestId: 'input' }));
  const waiting = await engine.update([sample(s)], { now: 2000 });
  assert.equal(waiting.sessions[0].state, 'waiting');
  const restarted = rig(waiting.sessions);
  assert.equal(restarted.engine.snapshot().sessions[0].state, 'unknown');
  const idle = await restarted.engine.update([sample(s, { busy: false })], { now: 3000 });
  assert.equal(idle.sessions[0].state, 'unknown');
  const fresh = await restarted.engine.update([sample(s)], { now: 4000 });
  assert.equal(fresh.sessions[0].state, 'waiting');
  assert.equal(restarted.alerts.length, 0);
  const missing = await restarted.engine.update([], { now: 5000 });
  assert.equal(missing.sessions[0].state, 'unknown');
});

test('retained session persistence fails visibly for corruption and serializes concurrent writes', async t => {
  const file = path.join(await temp(t), 'sessions.json');
  const { engine } = rig();
  const rows = (await engine.update([sample(state(start()))], { now: 1000 })).sessions;
  const store = new SessionStore(file);
  await Promise.all(Array.from({ length: 8 }, (_, i) => store.save(rows.map(row => ({ ...row, title: `Title ${i}` })))));
  assert.equal((await new SessionStore(file).load())[0].title, 'Title 7');
  for (const data of ['corrupt', JSON.stringify({ version: 1, sessions: [rows[0], rows[0]] }),
    JSON.stringify({ version: 1, sessions: [{ ...rows[0], state: 'finished' }] }),
    JSON.stringify({ version: 1, sessions: [{ ...rows[0], lastResponseAt: 'yesterday' }] })]) {
    await writeFile(file, data);
    await assert.rejects(new SessionStore(file).load());
  }
});
test('model/tool iteration end is never whole-run completion', async () => {
  const { engine, alerts } = rig();
  const s = state(start());
  await engine.update([sample(s)], { now: 1000 });
  s.accept(message([{ name: 'powershell' }], 'commentary'));
  s.accept(event('tool.execution_start', { toolCallId: 't', toolName: 'powershell' }));
  s.accept(event('tool.execution_complete', { toolCallId: 't', success: true, shellExecution: { exitCode: 0 } }));
  s.accept(end());
  assert.equal(s.terminal, null);
  await engine.update([sample(s)], { now: 2000 });
  s.accept(start('run-a', '1'));
  assert.equal((await engine.update([sample(s)], { now: 3000 })).active.length, 1);
  assert.equal(alerts.length, 0);
});

test('unphased final messages work, but intermediate chunks and commentary do not', () => {
  const final = event('assistant.message', { turnId: '0', toolRequests: [] });
  assert.ok(state(start(), final, end()).terminal);
  assert.equal(state(start(), message([], 'commentary'), end()).terminal, null);
  assert.equal(state(start(), event('assistant.message', {
    turnId: '0', toolRequests: [], chunkCount: 2, chunkIndex: 0,
  }), end()).terminal, null);
});

test('no recent output is not an idle or completion signal', async () => {
  const { engine, alerts } = rig();
  const s = state(start());
  for (let now = 0; now <= 120000; now += 5000) {
    assert.equal((await engine.update([sample(s)], { now })).active.length, 1);
  }
  assert.equal(alerts.length, 0);
});

for (const [prefix, label] of [
  ['permission', 'Permission needed'], ['user_input', 'Input needed'],
  ['exit_plan_mode', 'Plan approval needed'], ['elicitation', 'Input needed'],
]) {
  test(`${prefix} is distinct from working and completion, then resumes`, async () => {
    const { engine, alerts } = rig();
    const s = state(start());
    await engine.update([sample(s)], { now: 1000 });
    s.accept(event(`${prefix}.requested`, { requestId: 'request', toolCallId: 'tool' }));
    let view = await engine.update([sample(s)], { now: 2000 });
    assert.equal(view.active.length, 0);
    assert.equal(view.attention[0].detail, label);
    assert.equal(alerts[0].kind, 'waiting');
    await engine.update([sample(s)], { now: 3000 });
    assert.equal(alerts.length, 1);
    s.accept(event('tool.execution_complete', { toolCallId: 'tool' }));
    view = await engine.update([sample(s)], { now: 4000 });
    assert.equal(view.active.length, 1);
    assert.equal(view.attention.length, 0);
  });
}

test('ephemeral wait completion can be recovered from the next root turn', () => {
  const s = state(start(), event('permission.requested', { requestId: 'p' }), start('run-a', '1'));
  assert.equal(s.gates.size, 0);
});

test('resolved-by-hook permission never shows waiting', () => {
  const s = state(start(), event('permission.requested', { requestId: 'p', resolvedByHook: true }));
  assert.equal(s.gates.size, 0);
});

test('desktop ask_user external tools do not look like active execution', () => {
  const s = state(start(), event('tool.execution_start', { toolCallId: 'q', toolName: 'functions.ask_user' }),
    event('external_tool.requested', { toolCallId: 'q', requestId: 'r', toolName: 'ask_user' }));
  assert.equal(s.snapshot().waiting.kind, 'Input needed');
  s.accept(event('external_tool.completed', { requestId: 'r' }));
  s.accept(event('tool.execution_complete', { toolCallId: 'q' }));
  assert.equal(s.snapshot().waiting, null);
});

for (const type of ['abort', 'session.error']) {
  test(`${type} cannot become a successful completion`, async () => {
    const { engine, alerts } = rig();
    const s = state(start());
    await engine.update([sample(s)], { now: 1000 });
    s.accept(event(type));
    s.accept(message()); s.accept(end());
    const view = await engine.update([sample(s, { busy: false })], { now: 2000 });
    assert.equal(view.active.length, 0);
    assert.equal(alerts[0].kind, 'error');
    assert.equal(alerts.some(a => a.kind === 'finished'), false);
  });
}

for (const change of [
  { alive: false }, { owner: 'reused-pid:new-createdAt' }, { readError: 'Reader unavailable' },
  { interrupted: true },
]) {
  test(`failure cannot imply success: ${JSON.stringify(change)}`, async () => {
    const { engine, alerts } = rig();
    const s = state(start());
    await engine.update([sample(s)], { now: 1000 });
    s.accept(message()); s.accept(end());
    const view = await engine.update([sample(s, { busy: false, ...change })], { now: 2000 });
    assert.equal(view.active.length, 0);
    assert.equal(alerts.some(a => a.kind === 'finished'), false);
  });
}

test('source disconnection and sleep discard completion authority; reconnect baselines', async () => {
  for (const outage of [{ healthy: false, now: 2000 }, { now: 90000 }]) {
    const { engine, alerts } = rig();
    const s = state(start());
    await engine.update([sample(s)], { now: 1000 });
    s.accept(message()); s.accept(end());
    await engine.update([sample(s, { busy: false })], outage);
    await engine.update([sample(s, { busy: false })], { now: outage.now + 1000 });
    assert.equal(alerts.filter(a => a.kind === 'finished').length, 0);
    assert.equal(alerts[0].kind, 'warning');
  }
});

test('missing session, shutdown and rotation all fail closed', async () => {
  for (const kind of ['missing', 'shutdown', 'rotation']) {
    const { engine, alerts } = rig();
    const s = state(start());
    await engine.update([sample(s)], { now: 1000 });
    s.accept(message()); s.accept(end());
    if (kind === 'shutdown') s.accept(event('session.shutdown'));
    const row = sample(s, { busy: false });
    if (kind === 'rotation') row.events.replaced = true;
    await engine.update(kind === 'missing' ? [] : [row], { now: 2000 });
    assert.equal(alerts.some(a => a.kind === 'finished'), false);
  }
});

test('database idle before final JSONL flush does not lose completion evidence', async () => {
  const { engine, alerts } = rig();
  const s = state(start());
  await engine.update([sample(s)], { now: 1000 });
  const pending = await engine.update([sample(s, { busy: false })], { now: 2000 });
  assert.equal(pending.active.length, 0);
  assert.equal(alerts.length, 0);
  s.accept(message()); s.accept(end());
  await engine.update([sample(s, { busy: false })], { now: 3000 });
  assert.equal(alerts[0].kind, 'finished');
});

test('partial JSONL never confirms completion', async () => {
  const { engine, alerts } = rig();
  const s = state(start());
  await engine.update([sample(s)], { now: 1000 });
  s.accept(message()); s.accept(end());
  const row = sample(s, { busy: false });
  row.events.partial = true;
  assert.equal((await engine.update([row], { now: 2000 })).active.length, 0);
  assert.equal(alerts.length, 0);
});

test('standalone CLI model completion does not claim full run completion', async () => {
  const { engine, alerts } = rig();
  const s = state(start());
  assert.equal((await engine.update([sample(s, { source: 'CLI (activity only)' })], { now: 1000 })).active.length, 1);
  s.accept(message()); s.accept(end());
  const view = await engine.update([sample(s, { source: 'CLI (activity only)', busy: false })], { now: 2000 });
  assert.equal(view.active.length, 0);
  assert.equal(alerts.length, 0);
});

test('nested subagent events cannot finish root; replay event IDs are ignored', () => {
  const e = start();
  const s = state(e, e);
  const rootRun = s.runId;
  s.accept(event('assistant.turn_start', { turnId: 'child' }, { agentId: 'child' }));
  s.accept(event('session.task_complete', { success: true }, { agentId: 'child' }));
  assert.equal(s.runId, rootRun);
  assert.equal(s.terminal, null);
  assert.equal(JSON.stringify(s.snapshot()).includes('PRIVATE'), false);
});

test('task_complete is an explicit marker, but desktop must actually stop running', async () => {
  const { engine, alerts } = rig();
  const s = state(start());
  assert.equal((await engine.update([sample(s)], { now: 1000 })).active.length, 1);
  s.accept(event('session.task_complete', { success: true }));
  await engine.update([sample(s)], { now: 1500 });
  assert.equal(alerts.length, 0);
  await engine.update([sample(s, { busy: false })], { now: 2000 });
  assert.equal(alerts[0].kind, 'finished');
});

test('an already-terminal stale log cannot confirm a newly observed running flag clearing', async () => {
  const { engine, alerts } = rig();
  const s = state(start(), message(), end());
  await engine.update([sample(s)], { now: 1000 });
  await engine.update([sample(s)], { now: 1500 });
  const view = await engine.update([sample(s, { busy: false })], { now: 2000 });
  assert.equal(view.active.length, 0);
  assert.equal(view.attention[0].state, 'unknown');
  assert.equal(alerts.length, 0);
});

test('restart baselines historical completion and durable ledger deduplicates delivery', async t => {
  const dir = await temp(t);
  const file = path.join(dir, 'ledger.json');
  const ledger = new Ledger(file);
  await ledger.load();
  assert.equal(await ledger.claim('same-run:finished'), true);
  const restarted = new Ledger(file);
  await restarted.load();
  assert.equal(await restarted.claim('same-run:finished'), false);
  const persisted = await readFile(file, 'utf8');
  assert.equal(persisted.includes('same-run'), false);
  await writeFile(file, 'corrupt');
  await assert.rejects(new Ledger(file).load());
});

test('concurrent notification claims are serialized and persisted exactly once', async t => {
  const dir = await temp(t);
  const ledger = new Ledger(path.join(dir, 'notifications.json'));
  await ledger.load();
  const results = await Promise.all(Array.from({ length: 40 }, (_, n) => ledger.claim(`run-${n % 20}`)));
  assert.equal(results.filter(Boolean).length, 20);
  const afterRestart = new Ledger(ledger.file);
  await afterRestart.load();
  assert.equal(afterRestart.keys.size, 20);
  assert.equal(await afterRestart.claim('run-1'), false);
});

test('tail handles partial UTF-8 / JSON writes without retaining transcript content', async t => {
  const dir = await temp(t);
  const file = path.join(dir, 'events.jsonl');
  const first = start();
  const line = Buffer.from(JSON.stringify({ ...message(), data: { ...message().data, content: 'private \u2603' } }) + '\n');
  await writeFile(file, JSON.stringify(first) + '\n');
  const tail = new JsonlTail(file);
  assert.equal((await tail.read()).runId, first.id);
  const split = line.indexOf(Buffer.from('\u2603')) + 1;
  await appendFile(file, line.subarray(0, split));
  assert.equal((await tail.read()).partial, true);
  await appendFile(file, Buffer.concat([line.subarray(split), Buffer.from(JSON.stringify(end()) + '\n')]));
  const result = await tail.read();
  assert.ok(result.terminal);
  assert.equal(result.partial, false);
  assert.equal(JSON.stringify(result).includes('private'), false);
  assert.equal(tail.pending.length, 0);
});

test('tail detects replacement, truncation and same-inode rewrite/regrowth', async t => {
  const dir = await temp(t);
  const file = path.join(dir, 'events.jsonl');
  await writeFile(file, JSON.stringify(start()) + '\n');
  const tail = new JsonlTail(file);
  await tail.read();
  await rename(file, path.join(dir, 'old.jsonl'));
  await writeFile(file, JSON.stringify(start('new')) + '\n');
  assert.equal((await tail.read()).replaced, true);
  await writeFile(file, '');
  assert.equal((await tail.read()).replaced, true);
  await writeFile(file, JSON.stringify(start()) + '\n');
  await tail.read();
  await writeFile(file, [start('different'), message(), end()].map(e => JSON.stringify(e)).join('\n') + '\n');
  assert.equal((await tail.read()).replaced, true);
});

test('malformed complete lines fail closed and recover only after repair', async t => {
  const dir = await temp(t);
  const file = path.join(dir, 'events.jsonl');
  await writeFile(file, JSON.stringify(start()) + '\n{broken}\n');
  const tail = new JsonlTail(file);
  await assert.rejects(tail.read());
  assert.equal(tail.state.runId, null);
  await writeFile(file, JSON.stringify(start('repaired')) + '\n');
  assert.ok((await tail.read()).runId);
});
