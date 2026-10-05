const shellTool = /^(?:functions\.)?(powershell|read_powershell|stop_powershell)$/;
const shellId = '[A-Za-z0-9_.:-]+';

function shellResult(data) {
  const exit = data.result?.contents?.find(item => item.type === 'shell_exit');
  if (exit && Number.isInteger(exit.exitCode)) return { id: exit.shellId, exitCode: exit.exitCode };
  const text = typeof data.result?.content === 'string' ? data.result.content.trim() : '';
  // Match runtime envelopes, not arbitrary command output containing status-like text.
  const ended = text.match(new RegExp(`(?:^|\\n)<shellId: (${shellId}) completed with exit code (-?\\d+)>$`));
  if (ended) return { id: ended[1], exitCode: Number(ended[2]) };
  const running = text.match(new RegExp(`(?:^|\\n)<command with shellId: (${shellId}) is still running after \\d+ seconds\\.[^<>]*>$`));
  if (running) return { id: running[1], running: true };
  const moved = text.match(new RegExp(`(?:^|\\n)<command with shellId: (${shellId}) moved to background by the user\\.[^<>]*>$`));
  if (moved) return { id: moved[1], running: true };
  const started = text.match(new RegExp(`^<command started in (detached )?background with shellId: (${shellId})>$`));
  if (started) return { id: started[2], running: true, detached: Boolean(started[1]) };
  const stopped = text.match(new RegExp(`^<command with id: (${shellId}) stopped>$`));
  if (stopped) return { id: stopped[1], stopped: true };
  if (Number.isInteger(data.shellExecution?.exitCode)) return { exitCode: data.shellExecution.exitCode };
  return null;
}

export class BackgroundWork {
  constructor() { this.reset(); }

  reset() {
    this.calls = new Map();
    this.shells = new Map();
    this.agents = new Map();
    this.failure = null;
  }

  accept(event) {
    const d = event.data ?? {};
    if (['session.start', 'session.resume'].includes(event.type) && !event.agentId) this.reset();
    if (event.type === 'subagent.started' && event.agentId) {
      this.agents.set(event.agentId, event.timestamp);
    } else if (['subagent.completed', 'subagent.failed'].includes(event.type)) {
      const pending = this.agents.has(event.agentId);
      this.agents.delete(event.agentId);
      if (pending && (event.type === 'subagent.failed' || d.cancelled)) {
        this.failure = { id: event.id, kind: d.cancelled ? 'Background agent cancelled' : 'Background agent failed' };
      }
    } else if (event.type === 'assistant.turn_start') {
      if (event.agentId) this.agents.set(event.agentId, event.timestamp);
      else this.failure = null;
    } else if (event.type === 'system.notification') {
      const kind = d.kind;
      if (kind?.type === 'agent_completed' && kind.status === 'failed' && this.agents.has(kind.agentId)) {
        this.failure = { id: event.id, kind: 'Background agent failed' };
      }
      if (['agent_completed', 'agent_idle'].includes(kind?.type)) this.agents.delete(kind.agentId);
      if (kind?.type === 'shell_completed') this.finish(kind.shellId, kind.exitCode, event);
    } else if (event.type === 'tool.execution_start' && !d.mcpServerName && shellTool.test(d.toolName ?? '')) {
      this.calls.set(d.toolCallId, {
        name: d.toolName.replace(/^functions\./, ''), id: d.arguments?.shellId,
        detached: d.arguments?.detach === true, at: event.timestamp,
      });
    } else if (event.type === 'tool.execution_complete') {
      const call = this.calls.get(d.toolCallId);
      this.calls.delete(d.toolCallId);
      if (!call) return;
      const result = shellResult(d);
      const id = result?.id ?? call.id ?? `call:${d.toolCallId}`;
      const previous = this.shells.get(id);
      const detached = result?.detached ?? previous?.detached ?? call.detached;
      if (result && Number.isInteger(result.exitCode)) {
        this.finish(id, result.exitCode, event);
      } else if (result?.stopped && call.name === 'stop_powershell') {
        this.finish(id, -1, event);
      } else if (call.name === 'stop_powershell') {
        if (previous && !detached) {
          this.shells.set(id, { ...previous, state: 'unknown' });
        }
      } else if (result?.running) {
        this.shells.set(id, { detached, at: event.timestamp,
          state: call.name === 'powershell' || previous ? 'running' : 'unknown' });
      } else if (call.name === 'powershell' && d.success !== false) {
        this.shells.set(id, { detached, at: event.timestamp, state: 'unknown' });
      } else if (previous?.state === 'running') {
        this.shells.set(id, { ...previous, state: 'unknown' });
      }
    }
  }

  finish(id, exitCode, event) {
    const previous = this.shells.get(id);
    if (!previous) return;
    this.shells.set(id, { ...previous, state: Number.isInteger(exitCode) ? 'done' : 'unknown' });
    if (!previous.detached && Number.isInteger(exitCode) && exitCode !== 0) {
      this.failure = { id: event.id, kind: 'Background command exited unsuccessfully' };
    }
  }

  snapshot() {
    const shells = [...this.shells.values()].filter(item => !item.detached && item.state !== 'done');
    const running = shells.filter(item => item.state === 'running');
    const times = [...running.map(item => item.at), ...this.agents.values()];
    return {
      backgroundCount: running.length + this.agents.size,
      backgroundAt: times.sort().at(-1) ?? null,
      backgroundUnconfirmed: shells.some(item => item.state === 'unknown'),
      backgroundFailure: this.failure,
    };
  }
}
