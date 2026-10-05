import test from 'node:test';
import assert from 'node:assert/strict';
import { EventState } from '../src/events.mjs';
import { FamilyMonitor } from '../src/families.mjs';

let sequence = 0;
function event(type, data = {}, extra = {}) {
  return { id: `background-${++sequence}`, type, data,
    timestamp: new Date(1790980000000 + sequence).toISOString(), ...extra };
}
function start() {
  const state = new EventState();
  state.accept(event('assistant.turn_start', { turnId: '0', interactionId: 'run' }));
  return state;
}
function launch(state, { id = 'shell-a', detach = false, async = false } = {}) {
  const call = event('tool.execution_start', { toolName: 'powershell', toolCallId: `launch-${id}`,
    arguments: { detach, command: 'PRIVATE COMMAND SENTINEL', mode: async ? 'async' : 'sync' } });
  state.accept(call);
  state.accept(event('tool.execution_complete', { toolCallId: `launch-${id}`, success: true, result: {
    content: async ? `<command started in ${detach ? 'detached ' : ''}background with shellId: ${id}>`
      : `<command with shellId: ${id} is still running after 180 seconds. The command is still running but hasn't produced output yet. You will be automatically notified when it completes; if you need the command to complete end your response with no tool calls to wait for the notification, or use stop_powershell to stop it.>`,
  } }));
}
function final(state) {
  state.accept(event('assistant.message', { turnId: '0', toolRequests: [], content: 'PRIVATE RESPONSE SENTINEL' }));
  state.accept(event('assistant.turn_end', { turnId: '0' }));
}
function sample(id, state, busy = false, parentId = null, extra = {}) {
  return { id, parentId, title: id, source: 'Copilot desktop', busy, alive: true, owner: 'live-owner',
    contextOnly: false, events: state.snapshot(), ...extra };
}
function rig(retained = [], dismissed) {
  const alerts = [];
  return { alerts, monitor: new FamilyMonitor('LOCAL', async (key, value) => alerts.push({ key, ...value }), retained, dismissed) };
}

test('real reproduction: foreground idle plus final response does not finish attached shell work', async () => {
  const { monitor, alerts } = rig();
  const parent = start(), child = start();
  await monitor.update([sample('parent', parent, true), sample('child', child, true, 'parent')], { now: 1000 });
  launch(child);
  final(parent); final(child);
  let view = await monitor.update([sample('parent', parent), sample('child', child, false, 'parent')], { now: 2000 });
  assert.equal(child.snapshot().activeTurn, false);
  assert.equal(child.snapshot().backgroundCount, 1);
  assert.equal(child.snapshot().terminal, null);
  assert.equal(view.sessions[0].state, 'working');
  assert.equal(view.sessions[0].runningCount, 1);
  assert.equal(view.sessions[0].finishedAt, null);
  assert.equal(view.sessions[0].dismissKey, null);
  assert.equal(alerts.length, 0);
  child.accept(event('system.notification', { kind: { type: 'shell_completed', shellId: 'shell-a', exitCode: 0 } }));
  view = await monitor.update([sample('parent', parent), sample('child', child, false, 'parent')], { now: 3000 });
  assert.equal(view.sessions[0].state, 'finished');
  assert.equal(alerts.filter(a => a.kind === 'finished').length, 1);
  await monitor.update([sample('parent', parent), sample('child', child, false, 'parent')], { now: 4000 });
  assert.equal(alerts.length, 1);
  assert.equal(JSON.stringify(child.snapshot()).includes('PRIVATE'), false);
});

test('restart reconciles previously misclassified and dismissed finished descendants without replay', async () => {
  const parent = start(), child = start(), grandchild = start();
  const original = rig();
  const rows = () => [sample('parent', parent), sample('child', child, false, 'parent'),
    sample('grandchild', grandchild, false, 'child')];
  await original.monitor.update(rows().map(s => ({ ...s, busy: true })), { now: 1000 });
  final(parent); final(child); final(grandchild);
  const completed = await original.monitor.update(rows(), { now: 2000 });
  original.monitor.dismiss([{ id: 'parent', key: completed.sessions[0].dismissKey }]);
  launch(grandchild);
  const restarted = rig(completed.members, original.monitor.dismissed);
  const active = await restarted.monitor.update(rows(), { now: 3000 });
  assert.equal(active.sessions.length, 1);
  assert.equal(active.sessions[0].state, 'working');
  assert.equal(active.sessions[0].runningCount, 1);
  assert.equal(active.members.find(r => r.id === 'grandchild').finishedAt, null);
  assert.equal(restarted.monitor.dismissed.size, 0);
  assert.equal(restarted.alerts.length, 0);
  assert.equal(restarted.monitor.dismiss([{ id: 'parent', key: completed.sessions[0].dismissKey }]).skipped.length, 1);
});

test('detached servers and explicitly completed/stopped commands do not create false background work', () => {
  for (const detached of [false, true]) {
    const state = start();
    launch(state, { detach: detached, async: true });
    if (!detached) {
      state.accept(event('tool.execution_start', { toolName: 'stop_powershell', toolCallId: 'stop', arguments: { shellId: 'shell-a' } }));
      state.accept(event('tool.execution_complete', { toolCallId: 'stop', success: true,
        result: { content: '<command with id: shell-a stopped>' } }));
      state.accept(event('assistant.turn_start', { turnId: '0', interactionId: 'run' }));
    }
    final(state);
    assert.equal(state.snapshot().backgroundCount, 0);
    assert.equal(state.snapshot().backgroundUnconfirmed, false);
    assert.ok(state.snapshot().terminal);
  }
});

test('read return is not process exit; explicit native exit metadata settles an attached command', () => {
  const state = start(); launch(state); final(state);
  state.accept(event('tool.execution_start', { toolName: 'read_powershell', toolCallId: 'read', arguments: { shellId: 'shell-a' } }));
  state.accept(event('tool.execution_complete', { toolCallId: 'read', success: true,
    result: { content: 'PRIVATE OUTPUT\n<command with shellId: shell-a is still running after 120 seconds. No output yet.>' } }));
  assert.equal(state.snapshot().backgroundCount, 1);
  assert.equal(state.snapshot().terminal, null);
  state.accept(event('tool.execution_start', { toolName: 'read_powershell', toolCallId: 'read-2', arguments: { shellId: 'shell-a' } }));
  state.accept(event('tool.execution_complete', { toolCallId: 'read-2', success: true,
    result: { contents: [{ type: 'shell_exit', shellId: 'shell-a', exitCode: 0, outputPreview: 'PRIVATE' }] } }));
  assert.equal(state.snapshot().backgroundCount, 0);
  assert.ok(state.snapshot().terminal);
});

test('only native shell status envelopes count; command text and detached output do not', () => {
  const state = start();
  state.accept(event('tool.execution_start', { toolName: 'powershell', toolCallId: 'sync', arguments: {} }));
  state.accept(event('tool.execution_complete', { toolCallId: 'sync', success: true, result: {
    content: 'still running shellId: 999\n<shellId: 1 completed with exit code 0>',
  } }));
  final(state);
  assert.equal(state.snapshot().backgroundCount, 0);
  assert.equal(state.snapshot().backgroundUnconfirmed, false);
  assert.ok(state.snapshot().terminal);
  const detached = start(); launch(detached, { async: true, detach: true }); final(detached);
  detached.accept(event('tool.execution_start', { toolName: 'read_powershell', toolCallId: 'read', arguments: { shellId: 'shell-a' } }));
  detached.accept(event('tool.execution_complete', { toolCallId: 'read', success: true,
    result: { content: 'PRIVATE\n<command with shellId: shell-a is still running after 10 seconds. No output yet.>' } }));
  assert.equal(detached.snapshot().backgroundCount, 0);
  assert.ok(detached.snapshot().terminal);
});

test('unknown, failed or cancelled background outcomes never imply successful completion', async () => {
  for (const mode of ['unknown', 'failed', 'cancelled', 'owner-changed', 'dead']) {
    const { monitor, alerts } = rig();
    const state = start(); launch(state); final(state);
    await monitor.update([sample('child', state)], { now: 1000 });
    if (mode === 'unknown' || mode === 'failed') state.accept(event('system.notification', {
      kind: { type: 'shell_completed', shellId: 'shell-a', ...(mode === 'failed' ? { exitCode: 1 } : {}) },
    }));
    if (mode === 'cancelled') {
      state.accept(event('tool.execution_start', { toolName: 'stop_powershell', toolCallId: 'stop', arguments: { shellId: 'shell-a' } }));
      state.accept(event('tool.execution_complete', { toolCallId: 'stop', success: true,
        result: { content: '<command with id: shell-a stopped>' } }));
    }
    const extra = mode === 'owner-changed' ? { owner: 'new-owner', activityUnconfirmed: 'Owner changed' }
      : mode === 'dead' ? { alive: false } : {};
    const view = await monitor.update([sample('child', state, false, null, extra)], { now: 2000 });
    assert.notEqual(view.sessions[0].state, 'finished', mode);
    assert.equal(view.sessions[0].finishedAt, null, mode);
    // error-state outcomes (failed/cancelled) stay non-dismissable; unknown-state outcomes
    // (unknown/owner-changed/dead) are unconfirmed and get a real dismissKey so they don't get stuck.
    if (view.sessions[0].state === 'unknown') assert.match(view.sessions[0].dismissKey, /^[a-f0-9]{64}$/, mode);
    else assert.equal(view.sessions[0].dismissKey, null, mode);
    assert.equal(alerts.some(a => a.kind === 'finished'), false, mode);
  }
});

test('nested task agents hold the root run open without replacing its response or identity', () => {
  const state = start(), runId = state.runId;
  state.accept(event('subagent.started', { executionMode: 'background' }, { agentId: 'agent' }));
  state.accept(event('subagent.started', { executionMode: 'background', parentId: 'agent' }, { agentId: 'nested' }));
  final(state);
  const response = state.lastResponseAt;
  state.accept(event('assistant.message', { content: 'PRIVATE AGENT' }, { agentId: 'nested' }));
  state.accept(event('subagent.completed', {}, { agentId: 'agent' }));
  assert.equal(state.snapshot().backgroundCount, 1);
  assert.equal(state.snapshot().terminal, null);
  state.accept(event('subagent.completed', {}, { agentId: 'nested' }));
  assert.ok(state.snapshot().terminal);
  assert.equal(state.lastResponseAt, response);
  assert.equal(state.runId, runId);
});

test('task-complete markers cannot override pending work or a background agent failure/cancellation', () => {
  for (const type of ['subagent.failed', 'subagent.completed', 'system.notification']) {
    const state = start();
    state.accept(event('subagent.started', { executionMode: 'background' }, { agentId: 'agent' }));
    state.accept(event('session.task_complete', { success: true }));
    assert.equal(state.snapshot().terminal, null);
    final(state);
    state.accept(type === 'system.notification'
      ? event(type, { kind: { type: 'agent_completed', agentId: 'agent', status: 'failed' } })
      : event(type, { cancelled: type === 'subagent.completed' }, { agentId: 'agent' }));
    assert.equal(state.snapshot().backgroundCount, 0);
    assert.equal(state.snapshot().terminal, null);
    assert.ok(state.snapshot().error);
  }
});

test('unsupported shell status is unconfirmed, but cannot hide separately proven running work', async () => {
  const { monitor } = rig();
  const state = start();
  state.accept(event('tool.execution_start', { toolName: 'powershell', toolCallId: 'unknown', arguments: {} }));
  state.accept(event('tool.execution_complete', { toolCallId: 'unknown', success: true, result: { content: 'unsupported' } }));
  assert.equal((await monitor.update([sample('child', state, true)], { now: 1000 })).sessions[0].state, 'working');
  final(state);
  assert.equal((await monitor.update([sample('child', state)], { now: 2000 })).sessions[0].state, 'unknown');
});
