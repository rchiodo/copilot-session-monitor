import { open } from 'node:fs/promises';
import { BackgroundWork } from './background.mjs';

const MAX_LINE = 32 * 1024 * 1024;
const gateTypes = new Map([
  ['permission', 'Permission needed'],
  ['user_input', 'Input needed'],
  ['exit_plan_mode', 'Plan approval needed'],
  ['elicitation', 'Input needed'],
]);
const inputTool = /^(?:(?:functions|[^.]+)\.)?(ask_user|askUser|exit_plan_mode)$/;

export class EventState {
  constructor() {
    this.runId = null;
    this.startedAt = null;
    this.interactionId = null;
    this.turnId = null;
    this.activeTurn = false;
    this.closed = false;
    this.error = null;
    this.terminal = null;
    this.lastEventAt = null;
    this.lastResponseAt = null;
    this.lastExecutionId = null;
    this.tools = new Set();
    this.gates = new Map();
    this.finalMessage = false;
    this.finalTurnEnd = null;
    this.background = new BackgroundWork();
    this.lastExecutionAt = null;
    this.cwd = null;
    this.seen = new Set();
  }

  accept(event) {
    if (typeof event.id !== 'string' || typeof event.type !== 'string' ||
        !Number.isFinite(Date.parse(event.timestamp))) {
      throw new Error('Unsupported event envelope');
    }
    if (this.seen.has(event.id)) return;
    this.seen.add(event.id);
    if (this.seen.size > 4096) this.seen.delete(this.seen.values().next().value);
    const d = event.data ?? {};
    this.background.accept(event);
    // Subagents have their own turns; they cannot finish or start the root run.
    if (event.agentId || d.parentToolCallId) {
      this.settleBackground(event);
      return;
    }
    const type = event.type;
    this.lastEventAt = event.timestamp;
    if (type === 'session.start' || type === 'session.resume') {
      this.cwd = typeof d.context?.cwd === 'string' ? d.context.cwd : this.cwd;
      this.runId = null;
      this.activeTurn = false;
      this.closed = false;
      this.error = null;
      this.terminal = null;
      this.finalTurnEnd = null;
      this.tools.clear();
      this.gates.clear();
    } else if (type === 'assistant.turn_start') {
      const newRun = !this.runId || this.terminal || this.closed ||
        (d.interactionId && d.interactionId !== this.interactionId);
      if (newRun) {
        this.runId = event.id;
        this.startedAt = event.timestamp;
        this.tools.clear();
      }
      this.interactionId = d.interactionId ?? this.interactionId;
      this.turnId = d.turnId;
      this.activeTurn = true;
      this.closed = false;
      this.error = null;
      this.terminal = null;
      this.finalMessage = false;
      this.finalTurnEnd = null;
      this.gates.clear();
      this.lastExecutionId = event.id;
      this.lastExecutionAt = event.timestamp;
    } else if (type === 'assistant.message') {
      if (!this.lastResponseAt || Date.parse(event.timestamp) > Date.parse(this.lastResponseAt)) {
        this.lastResponseAt = event.timestamp;
      }
      if (d.turnId === this.turnId) {
        this.finalMessage = Array.isArray(d.toolRequests) && d.toolRequests.length === 0 &&
          (d.phase === undefined || d.phase === 'final_answer' || d.phase === 'final') &&
          (d.chunkCount === undefined || d.chunkIndex === d.chunkCount - 1);
      }
    } else if (type === 'assistant.turn_end' && d.turnId === this.turnId) {
      this.activeTurn = false;
      if (this.finalMessage && !this.tools.size && !this.gates.size && !this.error) {
        this.finalTurnEnd = { id: event.id, at: event.timestamp };
        this.settleBackground(event);
      }
    } else if (type === 'tool.execution_start') {
      this.tools.add(d.toolCallId);
      this.lastExecutionId = event.id;
      this.lastExecutionAt = event.timestamp;
      this.terminal = null;
      if (inputTool.test(d.toolName ?? '')) {
        this.gates.set(`tool:${d.toolCallId}`, {
          id: event.id, kind: /plan/.test(d.toolName) ? 'Plan approval needed' : 'Input needed',
          toolCallId: d.toolCallId,
        });
      }
    } else if (type === 'tool.execution_complete') {
      this.tools.delete(d.toolCallId);
      for (const [key, gate] of this.gates) {
        if (gate.toolCallId === d.toolCallId) this.gates.delete(key);
      }
    } else if (type === 'external_tool.requested' && inputTool.test(d.toolName ?? '')) {
      this.gates.set(`external:${d.requestId}`, {
        id: event.id, kind: /plan/.test(d.toolName) ? 'Plan approval needed' : 'Input needed',
        toolCallId: d.toolCallId,
      });
    } else if (type === 'external_tool.completed') {
      this.gates.delete(`external:${d.requestId}`);
    } else if (type === 'session.task_complete') {
      if (d.success === true && !this.error && !this.gates.size) {
        this.finalTurnEnd = { id: event.id, at: event.timestamp };
        this.settleBackground(event);
      } else if (d.success === false) {
        this.error = { id: event.id, kind: 'Run ended with an unsuccessful result' };
        this.terminal = null;
      }
    } else if (type === 'abort' || type === 'session.error') {
      this.error = { id: event.id, kind: type === 'abort' ? 'Run interrupted' : 'Session reported an error' };
      this.terminal = null;
      this.activeTurn = false;
    } else if (type === 'session.shutdown') {
      this.closed = true;
      this.activeTurn = false;
    } else {
      const [prefix, action] = type.split('.');
      if (gateTypes.has(prefix) && action === 'requested' && !d.resolvedByHook) {
        this.gates.set(`${prefix}:${d.requestId}`, {
          id: event.id, kind: gateTypes.get(prefix),
          toolCallId: d.toolCallId ?? d.permissionRequest?.toolCallId,
        });
      } else if (gateTypes.has(prefix) && action === 'completed') {
        this.gates.delete(`${prefix}:${d.requestId}`);
      }
    }
    if (type === 'system.notification' || type === 'tool.execution_complete') this.settleBackground(event);
  }

  settleBackground(event) {
    const work = this.background.snapshot();
    if (work.backgroundCount || work.backgroundUnconfirmed || work.backgroundFailure) {
      this.terminal = null;
    } else if (this.finalTurnEnd && !this.tools.size && !this.gates.size && !this.error) {
      this.terminal ??= { id: event.id, at: event.timestamp };
    }
  }

  snapshot() {
    const work = this.background.snapshot();
    return {
      ...work,
      runId: this.runId, startedAt: this.startedAt, activeTurn: this.activeTurn,
      closed: this.closed, error: this.error ?? (!this.activeTurn && this.finalTurnEnd ? work.backgroundFailure : null),
      terminal: work.backgroundCount || work.backgroundUnconfirmed ? null : this.terminal,
      lastEventAt: this.lastEventAt, lastExecutionId: this.lastExecutionId,
      lastExecutionAt: this.lastExecutionAt, activeTools: this.tools.size,
      lastResponseAt: this.lastResponseAt,
      cwd: this.cwd,
      waiting: this.gates.values().next().value ?? null,
      activity: work.backgroundCount ? 'Background work running' : this.tools.size ? 'Executing tools' : 'Agent running',
    };
  }
}

async function bytesAt(handle, start, length) {
  const buffer = Buffer.alloc(length);
  const { bytesRead } = await handle.read(buffer, 0, length, start);
  return buffer.subarray(0, bytesRead);
}

export class JsonlTail {
  constructor(file) {
    this.file = file;
    this.reset();
  }

  reset() {
    this.offset = 0;
    this.pending = Buffer.alloc(0);
    this.identity = null;
    this.anchor = Buffer.alloc(0);
    this.prefix = Buffer.alloc(0);
    this.state = new EventState();
  }

  async read() {
    const handle = await open(this.file, 'r');
    try {
      const stat = await handle.stat();
      const identity = `${stat.dev}:${stat.ino}:${stat.birthtimeMs}`;
      let replaced = false;
      if (this.identity) {
        const anchor = await bytesAt(handle, this.offset - this.anchor.length, this.anchor.length);
        const prefix = await bytesAt(handle, 0, this.prefix.length);
        replaced = identity !== this.identity || stat.size < this.offset ||
          !anchor.equals(this.anchor) || !prefix.equals(this.prefix);
        if (replaced) this.reset();
      }
      this.identity = identity;
      const initial = this.offset === 0;
      while (this.offset < stat.size) {
        const chunk = await bytesAt(handle, this.offset, Math.min(256 * 1024, stat.size - this.offset));
        if (!chunk.length) throw new Error('Event file changed during read');
        this.offset += chunk.length;
        const data = Buffer.concat([this.pending, chunk]);
        let start = 0;
        let end;
        while ((end = data.indexOf(10, start)) !== -1) {
          if (end - start > MAX_LINE) throw new Error('Event line exceeds supported size');
          if (end > start) this.state.accept(JSON.parse(data.subarray(start, end).toString('utf8')));
          start = end + 1;
        }
        this.pending = Buffer.from(data.subarray(start));
        if (this.pending.length > MAX_LINE) throw new Error('Partial event exceeds supported size');
      }
      this.anchor = await bytesAt(handle, Math.max(0, this.offset - 256), Math.min(256, this.offset));
      this.prefix = await bytesAt(handle, 0, Math.min(256, this.offset));
      return { ...this.state.snapshot(), replaced, initial, partial: this.pending.length > 0 };
    } catch (error) {
      this.reset();
      throw error;
    } finally {
      await handle.close();
    }
  }
}
