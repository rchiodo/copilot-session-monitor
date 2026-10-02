import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm, readFile } from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import { EventState } from '../src/events.mjs';
import { SessionStore, Ledger } from '../src/engine.mjs';
import { FamilyMonitor, groupFamilies } from '../src/families.mjs';
import { hierarchyIndex, selectedHierarchy, relatedMetadata, rootOf } from '../src/hierarchy.mjs';

let serial = 0;
function event(state, type, data = {}) {
  state.accept({ id: `e${++serial}`, timestamp: new Date(1790000000000 + serial * 1000).toISOString(), type, data });
}
function run() {
  const state = new EventState();
  event(state, 'assistant.turn_start', { interactionId: `run${serial}`, turnId: '0' });
  return state;
}
function finish(state) {
  event(state, 'assistant.message', { phase: 'final_answer', turnId: '0', content: 'PRIVATE TRANSCRIPT', toolRequests: [] });
  event(state, 'assistant.turn_end', { turnId: '0' });
}
const sample = (id, state, parentId = null, extra = {}) => ({
  id, title: `Title ${id}`, source: 'Copilot desktop', busy: true, alive: true, owner: 'pid:start',
  interrupted: false, events: state.snapshot(), parentId, hierarchyIssue: null, ...extra,
});
function rig(retained = []) {
  const alerts = [], keys = new Set();
  const monitor = new FamilyMonitor('TEST', async (key, alert) => {
    if (!keys.has(key)) { keys.add(key); alerts.push({ key, ...alert }); }
  }, retained);
  let now = 1000;
  return { monitor, alerts, step: (samples, options = {}) => monitor.update(samples, { now: now += 1000, ...options }) };
}

test('parent finishes while child works: one card, no early family notification, parent-only alert provenance', async () => {
  const { monitor, alerts, step } = rig();
  const parent = run(), child = run();
  await step([sample('parent', parent), sample('child', child, 'parent')]);
  finish(parent);
  let view = await step([sample('parent', parent, null, { busy: false }), sample('child', child, 'parent')]);
  assert.equal(view.sessions.length, 1);
  assert.equal(view.sessions[0].id, 'parent');
  assert.equal(view.sessions[0].state, 'working');
  assert.equal(view.sessions[0].runningCount, 1);
  assert.equal(view.sessions[0].parentAlert.kind, 'finished');
  assert.equal(view.sessions[0].parentAlert.sessionId, 'parent');
  assert.equal(alerts.length, 0);
  const parentAlert = view.sessions[0].parentAlert;
  finish(child);
  view = await step([sample('parent', parent, null, { busy: false }), sample('child', child, 'parent', { busy: false })]);
  assert.equal(view.sessions[0].state, 'finished');
  assert.deepEqual(view.sessions[0].parentAlert, parentAlert);
  assert.equal(monitor.rows.get('child').lastAlert.sessionId, 'child');
  assert.equal(alerts.length, 1);
  assert.equal(alerts[0].kind, 'finished');
  assert.equal(alerts[0].title, 'Title parent');
  await step([sample('parent', parent, null, { busy: false }), sample('child', child, 'parent', { busy: false })]);
  assert.equal(alerts.length, 1);
  assert.equal(JSON.stringify(view).includes('PRIVATE TRANSCRIPT'), false);
});

test('idle ancestor + child-only run + nested grandchild aggregate without reconstructing parent alert', async () => {
  const { alerts, step } = rig();
  const parent = run(), child = run(), grandchild = run();
  finish(parent);
  const rows = () => [sample('p', parent, null, { busy: false, contextOnly: true }),
    sample('c', child, 'p', { busy: !child.terminal }), sample('g', grandchild, 'c', { busy: !grandchild.terminal })];
  let view = await step(rows());
  assert.equal(view.sessions.length, 1);
  assert.equal(view.sessions[0].parentState, 'idle');
  assert.equal(view.sessions[0].parentAlert, null);
  assert.equal(view.sessions[0].childCount, 2);
  finish(child);
  view = await step(rows());
  assert.equal(view.sessions[0].state, 'working');
  assert.equal(alerts.length, 0);
  finish(grandchild);
  view = await step(rows());
  assert.equal(view.sessions[0].state, 'finished');
  assert.equal(view.sessions[0].parentAlert, null);
  assert.equal(alerts.length, 1);
});

test('families order by parent response, never descendant response or alert time; fallback is stable', async () => {
  const { step } = rig();
  const p = run(), c = run(), other = run();
  event(p, 'assistant.message', { phase: 'commentary' });
  event(other, 'assistant.message', { phase: 'commentary' });
  event(c, 'assistant.message', { phase: 'commentary' });
  const view = await step([sample('p', p), sample('c', c, 'p'), sample('independent', other)]);
  assert.deepEqual(view.sessions.map(row => row.id), ['independent', 'p']);
  assert.equal(view.sessions[1].lastResponseAt, p.lastResponseAt);
  assert.equal(groupFamilies(view.members).length, 2);
  const fallback = view.members.map(row => row.id === 'p' ? { ...row, lastResponseAt: null } : row);
  assert.equal(groupFamilies(fallback)[1].lastResponseAt, null);
  assert.equal(groupFamilies(fallback)[1].firstObservedAt, view.sessions[1].firstObservedAt);
});

test('parent waiting and error alerts update independently while its child works', async () => {
  const { step } = rig();
  const p = run(), c = run();
  await step([sample('p', p), sample('c', c, 'p')]);
  event(p, 'user_input.requested', { requestId: 'parent-question' });
  const waiting = await step([sample('p', p), sample('c', c, 'p')]);
  assert.equal(waiting.sessions[0].state, 'working');
  assert.equal(waiting.sessions[0].parentAlert.kind, 'waiting');
  event(p, 'assistant.turn_start', { interactionId: 'parent-resumed', turnId: '0' });
  await step([sample('p', p), sample('c', c, 'p')]);
  event(p, 'session.error');
  const failed = await step([sample('p', p, null, { busy: false }), sample('c', c, 'p')]);
  assert.equal(failed.sessions[0].state, 'working');
  assert.equal(failed.sessions[0].parentAlert.kind, 'error');
  assert.equal(failed.sessions[0].parentAlert.sessionId, 'p');
  assert.notEqual(failed.sessions[0].parentAlert.at, waiting.sessions[0].parentAlert.at);
});

test('late parent response metadata updates ordering without replacing the observed completion or alert', async () => {
  const { step, alerts } = rig();
  const p = run(), c = run();
  await step([sample('p', p), sample('c', c, 'p')]);
  finish(p);
  const first = await step([sample('p', p, null, { busy: false }), sample('c', c, 'p')]);
  event(p, 'assistant.message', { phase: 'final_answer' });
  const later = await step([sample('p', p, null, { busy: false }), sample('c', c, 'p')]);
  assert.equal(later.sessions[0].lastResponseAt, p.lastResponseAt);
  assert.deepEqual(later.sessions[0].parentAlert, first.sessions[0].parentAlert);
  assert.equal(later.members.find(row => row.id === 'p').finishedAt, first.members.find(row => row.id === 'p').finishedAt);
  event(p, 'session.error');
  finish(c);
  const failed = await step([sample('p', p, null, { busy: false }), sample('c', c, 'p', { busy: false })]);
  assert.equal(failed.sessions[0].state, 'error');
  assert.equal(failed.sessions[0].parentAlert.kind, 'error');
  assert.equal(alerts.some(alert => alert.kind === 'finished'), false);
});

for (const [kind, expected] of [['user_input.requested', 'waiting'], ['session.error', 'error'], ['abort', 'error']]) {
  test(`descendant ${kind} is not completion and does not overwrite parent alert`, async () => {
    const { alerts, step } = rig();
    const p = run(), c = run();
    await step([sample('p', p), sample('c', c, 'p')]);
    finish(p);
    await step([sample('p', p, null, { busy: false }), sample('c', c, 'p')]);
    event(c, kind, { requestId: 'question' });
    const view = await step([sample('p', p, null, { busy: false }), sample('c', c, 'p', { busy: false })]);
    assert.equal(view.sessions[0].state, expected);
    assert.equal(view.sessions[0].parentAlert.kind, 'finished');
    assert.equal(alerts.length, 1);
    assert.equal(alerts[0].kind, expected);
    assert.equal(alerts.some(alert => alert.kind === 'finished'), false);
  });
}

test('simultaneous child completions yield one family notification; new runs rearm once', async () => {
  const { alerts, step } = rig();
  const p = run(), a = run(), b = run();
  const rows = () => [sample('p', p, null, { busy: !p.terminal }),
    sample('a', a, 'p', { busy: !a.terminal }), sample('b', b, 'p', { busy: !b.terminal })];
  await step(rows());
  [p, a, b].forEach(finish);
  await step(rows());
  assert.equal(alerts.length, 1);
  event(a, 'assistant.turn_start', { interactionId: 'next', turnId: '0' });
  assert.equal((await step(rows())).sessions[0].state, 'working');
  finish(a);
  await step(rows());
  assert.equal(alerts.length, 2);
  assert.notEqual(alerts[0].key, alerts[1].key);
});

test('missing/dead/disconnected/rotated relatives and gaps never finish a family', async () => {
  for (const problem of ['missing', 'dead', 'disconnect', 'rotation', 'gap']) {
    const { alerts, step } = rig();
    const p = run(), c = run();
    await step([sample('p', p), sample('c', c, 'p')]);
    finish(p); finish(c);
    const rows = [sample('p', p, null, { busy: false }), sample('c', c, 'p', { busy: false })];
    if (problem === 'missing') rows.pop();
    if (problem === 'dead') rows[1].alive = false;
    if (problem === 'rotation') rows[1].events.replaced = true;
    const view = await step(rows, problem === 'disconnect' ? { healthy: false }
      : problem === 'gap' ? { now: 90000 } : {});
    assert.equal(view.sessions[0].state, 'unknown', problem);
    assert.equal(alerts.some(alert => alert.kind === 'finished'), false, problem);
  }
});

test('delayed terminal flush remains eligible, unlike lost completion authority', async () => {
  const { alerts, step } = rig();
  const p = run();
  await step([sample('p', p)]);
  assert.equal((await step([sample('p', p, null, { busy: false })])).sessions[0].state, 'unknown');
  finish(p);
  await step([sample('p', p, null, { busy: false })]);
  assert.equal(alerts[0].kind, 'finished');
});

test('detaching a working child never manufactures a completion for the old family', async () => {
  const { alerts, step } = rig();
  const p = run(), c = run();
  await step([sample('p', p), sample('c', c, 'p')]);
  finish(p);
  await step([sample('p', p, null, { busy: false }), sample('c', c, 'p')]);
  const detached = await step([sample('p', p, null, { busy: false }), sample('c', c)]);
  assert.equal(detached.sessions.length, 2);
  assert.equal(alerts.length, 0);
  finish(c);
  await step([sample('p', p, null, { busy: false }), sample('c', c, null, { busy: false })]);
  assert.equal(alerts.length, 1);
  assert.equal(alerts[0].title, 'Title c');
});

test('missing parent or cyclic hierarchy is visible and cannot generate completion', async () => {
  for (const cycle of [false, true]) {
    const { alerts, step } = rig();
    const a = run(), b = run();
    const rows = () => [sample('a', a, cycle ? 'b' : 'missing', { busy: !a.terminal }),
      ...(cycle ? [sample('b', b, 'a', { busy: !b.terminal })] : [])];
    const working = await step(rows());
    assert.equal(working.sessions.length, 1);
    assert.match(working.sessions[0].hierarchyIssue, cycle ? /Cycle/ : /missing/);
    finish(a); finish(b);
    assert.equal((await step(rows())).sessions[0].state, 'unknown');
    assert.equal(alerts.length, 0);
  }
});

test('family persistence retains relationships and exact parent alert; restart has no replay', async t => {
  const dir = await mkdtemp(path.join(os.tmpdir(), 'copilot-family-test-'));
  t.after(() => rm(dir, { recursive: true, force: true }));
  const store = new SessionStore(path.join(dir, 'sessions.json'));
  const ledger = new Ledger(path.join(dir, 'ledger.json'));
  await ledger.load();
  const delivered = [];
  const create = retained => new FamilyMonitor('TEST', async (key, alert) => {
    if (await ledger.claim(key)) delivered.push(alert);
  }, retained);
  const monitor = create();
  const p = run(), c = run();
  await monitor.update([sample('p', p), sample('c', c, 'p')], { now: 1000 });
  finish(p);
  const running = await monitor.update([sample('p', p, null, { busy: false }), sample('c', c, 'p')], { now: 2000 });
  await store.save(running.members);
  const restored = create(await new SessionStore(store.file).load());
  assert.equal(restored.snapshot().sessions[0].state, 'unknown');
  assert.deepEqual(restored.snapshot().sessions[0].parentAlert, running.sessions[0].parentAlert);
  finish(c);
  await restored.update([sample('p', p, null, { busy: false }), sample('c', c, 'p', { busy: false })], { now: 3000 });
  assert.equal(delivered.length, 0);
  event(c, 'assistant.turn_start', { interactionId: 'fresh', turnId: '0' });
  await restored.update([sample('p', p, null, { busy: false }), sample('c', c, 'p')], { now: 4000 });
  finish(c);
  const done = await restored.update([sample('p', p, null, { busy: false }), sample('c', c, 'p', { busy: false })], { now: 5000 });
  assert.equal(delivered.length, 1);
  await store.save(done.members);
  const again = create(await new SessionStore(store.file).load());
  await again.update([sample('p', p, null, { busy: false }), sample('c', c, 'p', { busy: false })], { now: 6000 });
  assert.equal(delivered.length, 1);
  assert.equal(again.snapshot().sessions[0].state, 'finished');
  assert.equal((await readFile(store.file, 'utf8')).includes('PRIVATE TRANSCRIPT'), false);
});

const schema = () => ({
  sessions: ['chat', 'old-parent', 'parent', 'child', 'nested', 'side', 'unrelated'].map(id => ({
    id, title: id, session_type: id === 'chat' ? 'general_chat' : 'project', execution_location: 'local', is_running: id === 'nested' ? 1 : 0,
  })),
  workspaces: [{ id: 'wp', session_id: 'parent', creator_session_id: 'chat', host_id: 'local' },
    { id: 'wc', session_id: 'child', creator_session_id: 'old-parent', host_id: 'local' },
    { id: 'wn', session_id: 'nested', creator_session_id: 'child', host_id: 'local' }],
  links: [{ child_workspace_id: 'wc', parent_workspace_id: 'wp' }, { child_workspace_id: 'wn', parent_workspace_id: 'wc' }],
  aliases: [{ session_id: 'old-parent', workspace_id: 'wp' }],
  workspaceChats: [], sessionChats: [],
});

test('canonical workspace IDs, runtime aliases, chat creators and nested ancestry resolve without history import', () => {
  const nodes = hierarchyIndex(schema());
  assert.equal(nodes.get('child').parentId, 'parent');
  assert.equal(nodes.get('old-parent').parentId, 'parent');
  assert.equal(nodes.get('parent').parentId, 'chat');
  assert.equal(rootOf('nested', nodes).id, 'chat');
  assert.deepEqual(selectedHierarchy(nodes, new Set()).map(row => row.id).sort(), ['chat', 'child', 'nested', 'parent']);
  assert.equal(selectedHierarchy(nodes, new Set()).some(row => row.id === 'unrelated'), false);
  assert.equal(relatedMetadata(nodes, selectedHierarchy(nodes, new Set())).some(row => row.id === 'old-parent'), false);
});

test('recorded side chats, missing links, cycles, detach and remote relatives are conservative', () => {
  const data = schema();
  data.workspaceChats.push({ workspace_id: 'wc', session_id: 'side' });
  let nodes = hierarchyIndex(data);
  assert.equal(nodes.get('side').parentId, 'child');
  data.sessionChats.push({ parent_session_id: 'chat', session_id: 'unrelated' });
  assert.equal(hierarchyIndex(data).get('unrelated').parentId, 'chat');
  data.links[0].parent_workspace_id = 'gone';
  nodes = hierarchyIndex(data);
  assert.match(nodes.get('child').hierarchyIssue, /missing/);
  assert.match(nodes.get('gone').title, /Unavailable parent/);
  data.links[0].parent_workspace_id = 'wn';
  assert.match(rootOf('nested', hierarchyIndex(data)).issue, /Cycle/);
  data.links = [];
  data.workspaces[1].creator_session_id = null;
  assert.equal(hierarchyIndex(data).get('child').parentId, null);
  data.workspaces[2].host_id = 'remote-host';
  nodes = hierarchyIndex(data);
  assert.equal(selectedHierarchy(nodes, new Set(['child'])).find(row => row.id === 'nested').local, false);
});

test('ambiguous identity aliases and conflicting recorded parents are surfaced', () => {
  const data = schema();
  data.aliases.push({ session_id: 'old-parent', workspace_id: 'wc' });
  data.sessionChats.push({ session_id: 'side', parent_session_id: 'old-parent' });
  assert.match(hierarchyIndex(data).get('side').hierarchyIssue, /Ambiguous/);
  data.workspaceChats.push({ session_id: 'side', workspace_id: 'wn' });
  assert.match(hierarchyIndex(data).get('side').hierarchyIssue, /Conflicting/);
});
