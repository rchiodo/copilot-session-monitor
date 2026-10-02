import { createHash } from 'node:crypto';
import { MonitorEngine, sortSessions } from './engine.mjs';
import { rootOf } from './hierarchy.mjs';

export function familyDetails(rootId, members, relatives) {
  const nodes = new Map(relatives.map(row => [row.id, { ...row, state: 'unobserved' }]));
  for (const row of members) nodes.set(row.id, { ...nodes.get(row.id), ...row });
  const selected = [...nodes.values()].filter(row => rootOf(row.id, nodes).id === rootId);
  const ordered = [], visited = new Set();
  const walk = (id, depth) => {
    if (visited.has(id)) return;
    visited.add(id);
    const row = nodes.get(id);
    if (row) ordered.push({ ...row, depth });
    for (const child of selected.filter(row => row.parentId === id).sort((a, b) => a.title.localeCompare(b.title))) {
      walk(child.id, depth + 1);
    }
  };
  walk(rootId, 0);
  for (const row of members) if (!visited.has(row.id)) ordered.push({ ...row, depth: 1 });
  return ordered;
}

export function groupFamilies(rows, relatives = []) {
  const nodes = new Map(rows.map(row => [row.id, row]));
  const groups = new Map();
  for (const row of rows) {
    const root = rootOf(row.id, nodes);
    const group = groups.get(root.id) ?? { rows: [], issue: root.issue };
    group.rows.push(row);
    group.issue ??= row.hierarchyIssue ?? root.issue;
    groups.set(root.id, group);
  }
  return sortSessions([...groups].map(([id, group]) => {
    const parent = nodes.get(id) ?? {
      ...group.rows[0], id, title: `Unavailable parent ${id.slice(0, 8)}`,
      state: 'unknown', lastResponseAt: null, lastAlert: null, finishedAt: null,
    };
    const working = group.rows.filter(row => row.state === 'working');
    const observed = group.rows.filter(row => !row.contextOnly);
    const unknown = group.rows.some(row => row.state === 'unknown') || Boolean(group.issue);
    const state = working.length ? 'working' : unknown ? 'unknown'
      : group.rows.some(row => row.state === 'error') ? 'error'
        : group.rows.some(row => row.state === 'waiting') ? 'waiting'
          : observed.length && observed.every(row => row.state === 'finished') ? 'finished' : 'unknown';
    const finishedAt = state === 'finished'
      ? observed.map(row => row.finishedAt).filter(Boolean).sort().at(-1) : null;
    const dismissKey = state === 'finished' && !group.issue ? createHash('sha256').update(JSON.stringify(group.rows
      .map(row => [row.id, row.parentId ?? null, row.runId, row.startedAt, row.finishedAt, Boolean(row.contextOnly)])
      .sort((a, b) => a[0].localeCompare(b[0])))).digest('hex') : null;
    return {
      ...parent, state, finishedAt, hierarchyIssue: group.issue,
      detail: state === 'working' ? `${working.length} observed family member(s) working`
        : state === 'finished' ? 'All observed family runs finished; not task or PR success'
          : group.issue ?? (state === 'unknown' ? 'Family completion unconfirmed; inspect member status'
            : state === 'waiting' ? 'Family needs input or approval' : 'Family has an error or interruption'),
      parentAlert: parent.lastAlert ?? null, parentState: parent.state,
      runningCount: working.length, childCount: group.rows.filter(row => row.id !== id).length,
      members: sortSessions(group.rows),
      relatives: familyDetails(id, group.rows, relatives), dismissKey,
    };
  }));
}

export class FamilyMonitor {
  constructor(machine, emit, retained = [], dismissed = new Map()) {
    this.emit = emit;
    this.pending = [];
    this.engine = new MonitorEngine(machine, async (key, alert) => this.pending.push({ key, ...alert }), retained);
    this.armed = new Map();
    this.dismissed = new Map(dismissed);
    this.relatives = [];
    this.startupHidden = new Set(groupFamilies(retained)
      .filter(row => row.dismissKey && this.dismissed.get(row.id) === row.dismissKey).map(row => row.id));
  }

  get rows() { return this.engine.rows; }
  get observed() { return this.engine.observed; }

  snapshot(gap = false) {
    const members = sortSessions(this.rows.values());
    const sessions = groupFamilies(members, this.relatives)
      .filter(row => !this.startupHidden.has(row.id) && (!row.dismissKey || this.dismissed.get(row.id) !== row.dismissKey));
    return { members, sessions, active: sessions.filter(row => row.state === 'working'),
      attention: sessions.filter(row => ['waiting', 'error', 'unknown'].includes(row.state)), gap };
  }

  dismiss(entries) {
    if (!Array.isArray(entries) || !entries.length || entries.length > 1000 ||
        entries.some(entry => typeof entry?.id !== 'string' || entry.id.length > 200 ||
          typeof entry.key !== 'string' || !/^[a-f0-9]{64}$/.test(entry.key))) {
      throw new TypeError('Expected 1-1000 finished family IDs and revision keys');
    }
    const current = new Map(groupFamilies([...this.rows.values()], this.relatives).map(row => [row.id, row]));
    const result = { dismissed: [], skipped: [] };
    for (const entry of new Map(entries.map(entry => [entry.id, entry])).values()) {
      const row = current.get(entry.id);
      if (!row?.dismissKey || row.dismissKey !== entry.key) {
        result.skipped.push({ id: entry.id, reason: 'No longer the same safely finished family' });
      } else {
        this.dismissed.set(entry.id, row.dismissKey);
        result.dismissed.push(entry.id);
      }
    }
    return result;
  }

  async update(samples, options = {}) {
    this.pending = [];
    const wasBaseline = this.engine.baseline;
    const result = await this.engine.update(samples, options);
    this.startupHidden.clear();
    if (options.relatives) this.relatives = options.relatives;
    const at = new Date(options.now ?? Date.now()).toISOString();
    const changed = [];
    for (const event of this.pending) {
      const row = this.rows.get(event.sessionId);
      if (!row || row.lastAlert?.key === event.key) continue;
      const lastAlert = { sessionId: row.id, key: event.key, kind: event.kind, message: event.message, at };
      this.rows.set(row.id, { ...row, lastAlert });
      changed.push(event);
    }
    const families = groupFamilies([...this.rows.values()], this.relatives);
    const interrupted = options.healthy === false || result.gap;
    if (interrupted) this.armed.clear();
    for (const family of families) {
      if (['working', 'waiting', 'error'].includes(family.state) ||
          (family.dismissKey && this.dismissed.get(family.id) !== family.dismissKey)) this.dismissed.delete(family.id);
      const membership = family.members.map(row => row.id).sort().join('|');
      const invalid = interrupted || family.hierarchyIssue || family.members.some(row =>
        row.state === 'unknown' && !this.observed.has(row.id));
      if (invalid) this.armed.delete(family.id);
      else if (family.state === 'working') this.armed.set(family.id, membership);
      else if (this.armed.get(family.id) !== membership) this.armed.delete(family.id);
      const events = changed.filter(event => family.members.some(row => row.id === event.sessionId));
      const notices = events.filter(event => event.kind !== 'finished');
      if (notices.length && (!wasBaseline || interrupted)) {
        const kind = ['error', 'waiting', 'warning'].find(kind => notices.some(event => event.kind === kind));
        const key = `family:${family.id}:${notices.map(event => event.key).sort().join('|')}`;
        await this.emit(key, { kind, title: family.title,
          message: `${notices.length} family member alert(s). ${notices.find(event => event.kind === kind).message}` });
      }
      if (!invalid && family.state === 'finished' && this.armed.has(family.id) && !wasBaseline) {
        const runs = family.members.filter(row => !row.contextOnly).map(row => [row.id, row.runId]).sort();
        const key = createHash('sha256').update(JSON.stringify(runs)).digest('hex');
        await this.emit(`family:${family.id}:${key}:finished`, {
          kind: 'finished', title: family.title,
          message: 'All observed family runs finished. This does not mean the entire task or PR succeeded.',
        });
        this.armed.delete(family.id);
      }
    }
    const ids = new Set(families.map(row => row.id));
    for (const id of this.armed.keys()) if (!ids.has(id)) this.armed.delete(id);
    for (const id of this.dismissed.keys()) if (!ids.has(id)) this.dismissed.delete(id);
    return this.snapshot(result.gap);
  }
}
