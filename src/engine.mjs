import { createHash } from 'node:crypto';
import { readFile, mkdir, writeFile, rename } from 'node:fs/promises';
import path from 'node:path';

export class Ledger {
  constructor(file) {
    this.file = file;
    this.keys = new Set();
    this.writes = Promise.resolve();
  }

  async load() {
    try {
      const data = JSON.parse(await readFile(this.file, 'utf8'));
      if (data.version !== 1 || !Array.isArray(data.keys) ||
          !data.keys.every(key => typeof key === 'string' && /^[a-f0-9]{64}$/.test(key))) {
        throw new Error('Invalid notification ledger');
      }
      this.keys = new Set(data.keys);
    } catch (error) {
      if (error.code !== 'ENOENT') throw error;
    }
  }

  claim(key) {
    return this.claimDigest(createHash('sha256').update(key).digest('hex'));
  }

  claimDigest(hash) {
    if (!/^[a-f0-9]{64}$/.test(hash)) throw new TypeError('Invalid notification digest');
    const result = this.writes.then(() => this.claimOnce(hash));
    // A failed write must reach its caller without poisoning subsequent writes.
    this.writes = result.then(() => undefined, () => undefined);
    return result;
  }

  async claimOnce(hash) {
    if (this.keys.has(hash)) return false;
    const keys = [...this.keys, hash].slice(-4096);
    await mkdir(path.dirname(this.file), { recursive: true });
    await writeFile(`${this.file}.tmp`, JSON.stringify({ version: 1, keys }), 'utf8');
    await rename(`${this.file}.tmp`, this.file);
    this.keys = new Set(keys);
    return true;
  }
}

export function sortSessions(rows) {
  return [...rows].sort((a, b) =>
    Date.parse(b.lastResponseAt ?? b.firstObservedAt) - Date.parse(a.lastResponseAt ?? a.firstObservedAt) ||
    a.id.localeCompare(b.id));
}

export function unavailableSessions(rows, reason) {
  return rows.map(row => {
    const result = ['working', 'waiting', 'idle'].includes(row.state)
      ? { ...row, state: 'unknown', detail: reason, finishedAt: null } : { ...row };
    if (row.members) {
      result.members = unavailableSessions(row.members, reason);
      if (row.relatives) result.relatives = unavailableSessions(row.relatives, reason);
      result.runningCount = 0;
    }
    return result;
  });
}

const rowFields = [
  'id', 'title', 'machine', 'source', 'state', 'detail', 'activity', 'runId',
  'firstObservedAt', 'startedAt', 'lastEventAt', 'lastResponseAt', 'finishedAt',
  'parentId', 'hierarchyIssue', 'contextOnly', 'lastAlert',
];
function storedRow(row) {
  row = { parentId: null, hierarchyIssue: null, contextOnly: false, lastAlert: null, ...row };
  for (const key of ['id', 'title', 'machine', 'source', 'state', 'detail', 'activity']) {
    if (typeof row[key] !== 'string') throw new Error(`Invalid retained session field: ${key}`);
  }
  if (!['working', 'finished', 'waiting', 'error', 'unknown', 'idle'].includes(row.state) ||
      !(row.runId === null || typeof row.runId === 'string')) throw new Error('Invalid retained session state');
  for (const key of ['firstObservedAt', 'startedAt', 'lastEventAt', 'lastResponseAt', 'finishedAt']) {
    if (key !== 'firstObservedAt' && row[key] === null) continue;
    if (typeof row[key] !== 'string' || !Number.isFinite(Date.parse(row[key]))) {
      throw new Error(`Invalid retained session timestamp: ${key}`);
    }
  }
  if ((row.state === 'finished') !== (row.finishedAt !== null)) throw new Error('Invalid retained completion time');
  for (const key of ['parentId', 'hierarchyIssue']) {
    if (row[key] !== null && typeof row[key] !== 'string') throw new Error(`Invalid retained ${key}`);
  }
  if (typeof row.contextOnly !== 'boolean') throw new Error('Invalid retained context flag');
  if (row.lastAlert !== null) {
    const alert = row.lastAlert;
    if (alert.sessionId !== row.id || !['finished', 'waiting', 'error', 'warning'].includes(alert.kind) ||
        typeof alert.key !== 'string' || typeof alert.message !== 'string' ||
        typeof alert.at !== 'string' || !Number.isFinite(Date.parse(alert.at))) throw new Error('Invalid retained parent alert');
    row.lastAlert = Object.fromEntries(['sessionId', 'key', 'kind', 'message', 'at'].map(key => [key, alert[key]]));
  }
  return Object.fromEntries(rowFields.map(key => [key, row[key]]));
}

export class SessionStore {
  constructor(file) {
    this.file = file;
    this.serialized = null;
    this.dismissed = new Map();
    this.writes = Promise.resolve();
  }

  async load() {
    try {
      const data = JSON.parse(await readFile(this.file, 'utf8'));
      if (![1, 2, 3].includes(data.version) || !Array.isArray(data.sessions)) throw new Error('Invalid retained session store');
      const rows = data.sessions.map(storedRow);
      if (new Set(rows.map(row => row.id)).size !== rows.length) throw new Error('Duplicate retained session IDs');
      const dismissed = data.version === 3 ? data.dismissed : [];
      if (!Array.isArray(dismissed) || dismissed.some(entry => typeof entry?.id !== 'string' ||
          typeof entry.key !== 'string' || !/^[a-f0-9]{64}$/.test(entry.key)) ||
          new Set(dismissed.map(entry => entry.id)).size !== dismissed.length) throw new Error('Invalid dismissed family markers');
      this.dismissed = new Map(dismissed.map(entry => [entry.id, entry.key]));
      this.serialized = JSON.stringify({ version: data.version, sessions: sortSessions(rows) });
      return rows;
    } catch (error) {
      if (error.code !== 'ENOENT') throw error;
      return [];
    }
  }

  save(rows, dismissed = this.dismissed) {
    const serialized = JSON.stringify({ version: 3, sessions: sortSessions(rows).map(storedRow),
      dismissed: [...dismissed].map(([id, key]) => ({ id, key })) });
    const result = this.writes.then(async () => {
      if (serialized === this.serialized) return;
      await mkdir(path.dirname(this.file), { recursive: true });
      await writeFile(`${this.file}.tmp`, serialized, 'utf8');
      await rename(`${this.file}.tmp`, this.file);
      this.serialized = serialized;
    });
    this.writes = result.then(() => undefined, () => undefined);
    return result;
  }
}

export class MonitorEngine {
  constructor(machine, emit, retained = []) {
    this.machine = machine;
    this.emit = emit;
    this.observed = new Map();
    this.rows = new Map(unavailableSessions(retained, 'Monitor restarted; current run status is unconfirmed')
      .map(row => [row.id, row]));
    this.lastPoll = null;
    this.baseline = true;
  }

  snapshot(gap = false) {
    const sessions = sortSessions(this.rows.values());
    return {
      sessions, active: sessions.filter(row => row.state === 'working'),
      attention: sessions.filter(row => ['waiting', 'error', 'unknown'].includes(row.state)), gap,
    };
  }

  async update(samples, { healthy = true, now = Date.now(), reason = 'Observation unavailable' } = {}) {
    const gap = this.lastPoll !== null && (now - this.lastPoll > 15000 || now < this.lastPoll);
    this.lastPoll = now;
    if (!healthy || gap) {
      const detail = gap ? 'Observation gap (sleep or pause)' : reason;
      this.rows = new Map(unavailableSessions([...this.rows.values()], detail).map(row => [row.id, row]));
      for (const [id, previous] of this.observed) {
        await this.emit(`${id}:${previous.runId}:offline`, {
          sessionId: id, kind: 'warning', title: previous.card.title, message: `${detail}. Completion was not confirmed.`,
        });
      }
      this.observed.clear();
      this.baseline = true;
      return this.snapshot(gap);
    }
    const present = new Set();
    for (const sample of samples) {
      const { id, events: e } = sample;
      const busy = sample.busy || Boolean(e?.backgroundCount && !sample.activityUnconfirmed);
      const unresolved = sample.activityUnconfirmed || sample.completionUnconfirmed || e?.backgroundUnconfirmed;
      present.add(id);
      const previous = this.observed.get(id);
      let saved = this.rows.get(id);
      if (saved && 'parentId' in sample) {
        saved = { ...saved, parentId: sample.parentId, hierarchyIssue: sample.hierarchyIssue ?? null, title: sample.title };
        this.rows.set(id, saved);
      }
      const responseTimes = [saved?.lastResponseAt, e?.lastResponseAt].filter(Boolean);
      const card = {
        id, title: sample.title, machine: this.machine, source: sample.source,
        runId: e?.runId ?? saved?.runId ?? null,
        startedAt: e?.startedAt ?? saved?.startedAt ?? null,
        lastEventAt: e?.lastEventAt ?? saved?.lastEventAt ?? null,
        lastResponseAt: responseTimes.sort((a, b) => Date.parse(b) - Date.parse(a))[0] ?? null,
        firstObservedAt: saved?.firstObservedAt ?? new Date(now).toISOString(),
        activity: e?.activity ?? saved?.activity ?? 'Agent running',
        parentId: sample.parentId ?? saved?.parentId ?? null,
        hierarchyIssue: sample.hierarchyIssue ?? saved?.hierarchyIssue ?? null,
        contextOnly: saved?.contextOnly ?? sample.contextOnly ?? false,
        lastAlert: saved?.lastAlert ?? null,
      };
      const retain = (state, detail, finishedAt = null) => {
        this.rows.set(id, { ...card, state, detail, finishedAt, contextOnly: state === 'working' ? false : card.contextOnly });
      };
      const lose = async (detail) => {
        if ((saved && (saved.state !== 'finished' || busy || unresolved || e?.backgroundCount ||
            e && e.runId !== saved.runId || Date.parse(e?.lastExecutionAt) > Date.parse(saved.lastEventAt))) ||
            sample.contextOnly || busy || unresolved) retain('unknown', detail);
        if (previous) {
          await this.emit(`${id}:${previous.runId}:offline`, {
            sessionId: id, kind: 'warning', title: card.title, message: `${detail}. Completion was not confirmed.`,
          });
        }
        this.observed.delete(id);
      };
      if (!sample.alive || !e || sample.readError || e.closed || e.replaced ||
          (unresolved && !busy) || (previous && previous.owner !== sample.owner)) {
        await lose(sample.readError ?? sample.activityUnconfirmed ?? sample.completionUnconfirmed ?? (unresolved
          ? 'Outstanding background work has unconfirmed status'
          : e?.replaced ? 'Event file rotated; rebaselining' : 'Session process unavailable or changed'));
        continue;
      }
      if (!busy && !previous && card.contextOnly) {
        retain(e.waiting || e.error || sample.interrupted || e.partial ? 'unknown' : 'idle',
          e.waiting || e.error || sample.interrupted || e.partial
            ? 'Related session has unresolved evidence; current status is unconfirmed'
            : 'Parent/ancestor observed idle; no completion alert inferred');
        continue;
      }
      if (e.waiting) {
        if (previous || busy || saved?.state === 'waiting') retain('waiting', e.waiting.kind);
        else if (saved) retain('unknown', 'A saved input gate exists; current waiting status is unconfirmed');
        if (!this.baseline && previous) {
          await this.emit(`${id}:${e.waiting.id}:waiting`, {
            sessionId: id, kind: 'waiting', title: card.title, message: `${e.waiting.kind}. The run is not finished.`,
          });
        }
        continue;
      }
      if (e.error || sample.interrupted) {
        const detail = e.error?.kind ?? 'Run interrupted';
        if (saved || busy) retain('error', detail);
        if (!this.baseline && (previous || (saved?.state === 'finished' && e.runId === saved.runId))) {
          await this.emit(`${id}:${e.error?.id ?? previous?.runId ?? e.runId}:error`, {
            sessionId: id, kind: 'error', title: card.title, message: `${detail}. This is not a successful completion.`,
          });
        }
        this.observed.delete(id);
        continue;
      }
      // Refresh response metadata without inferring a new completion after restart.
      if (!busy && !previous && saved?.state === 'finished' && e.runId === saved.runId &&
          e.terminal && (!e.lastExecutionAt || Date.parse(e.lastExecutionAt) <= Date.parse(saved.lastEventAt))) {
        this.rows.set(id, { ...saved, ...card });
        continue;
      }
      if (busy && e.runId && !e.partial) {
        if (sample.source === 'CLI (activity only)' && !e.activeTurn) {
          if (saved) retain('unknown', 'Model turn ended; full CLI run status is unavailable');
          this.observed.delete(id);
          continue;
        }
        const terminalAtObservation = previous?.runId === e.runId
          ? previous.terminalAtObservation : e.terminal?.id ?? null;
        retain('working', e.activity);
        this.observed.set(id, { runId: e.runId, owner: sample.owner, card, terminalAtObservation });
      } else if (busy && e.partial && previous && saved?.state === 'working') {
        // Retain existing execution evidence while the next JSONL record is incomplete.
      } else if (!busy && previous &&
          (sample.source === 'Copilot desktop' || sample.source === 'CLI (activity only)')) {
        if (e.terminal && !e.partial && e.runId === previous.runId &&
            e.terminal.id !== previous.terminalAtObservation) {
          await this.emit(`${id}:${e.runId}:finished`, {
            sessionId: id, kind: 'finished', title: card.title,
            message: 'Current agent run finished. This does not mean the entire task or PR succeeded.',
          });
          retain('finished', 'Current run finished; this is not task or PR success', new Date(now).toISOString());
          this.observed.delete(id);
        } else {
          retain('unknown', 'Not running; waiting for explicit completion evidence');
        }
      } else if (saved) {
        if (saved.state !== 'error') retain('unknown', busy
          ? 'Current execution evidence is incomplete' : 'Not running; full run completion was not observed');
        this.observed.delete(id);
      }
    }
    for (const [id, row] of this.rows) {
      if (!present.has(id) && ['working', 'waiting', 'idle'].includes(row.state)) {
        this.rows.set(id, { ...row, state: 'unknown', detail: 'Session no longer observable', finishedAt: null });
      }
    }
    for (const [id, previous] of this.observed) {
      if (!present.has(id)) {
        await this.emit(`${id}:${previous.runId}:offline`, {
          sessionId: id, kind: 'warning', title: previous.card.title, message: 'Session no longer observable. Completion was not confirmed.',
        });
        this.observed.delete(id);
      }
    }
    this.baseline = false;
    return this.snapshot();
  }
}
