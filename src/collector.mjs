import { randomBytes } from 'node:crypto';
import { readFile, writeFile, rename } from 'node:fs/promises';
import { groupFamilies } from './families.mjs';
import { sortSessions } from './engine.mjs';
import { HEARTBEAT_MS, digest, fail, metadata, validateConnect, validateReport } from './protocol.mjs';

const namespace = (source, id) => `${source}~${id}`;
const membership = family => family.members.map(row => row.id).sort().join('|');
const unknown = (row, reason) => ({ ...row, state: 'unknown', finishedAt: null, completionTracked: false, detail: reason });
function mapped(row, source) {
  const convert = alert => alert ? { ...alert, sessionId: namespace(source.id, alert.sessionId) } : null;
  return { ...row, id: namespace(source.id, row.id),
    parentId: row.parentId ? namespace(source.id, row.parentId) : null,
    aliasOf: row.aliasOf ? namespace(source.id, row.aliasOf) : null,
    machine: source.label, reporterId: source.id, machineTag: `${source.label} (${source.id.slice(0, 8)})`,
    lastAlert: convert(row.lastAlert), parentAlert: convert(row.parentAlert),
    ...(row.members ? { members: row.members.map(member => mapped(member, source)) } : {}),
    ...(row.relatives ? { relatives: row.relatives.map(member => mapped(member, source)) } : {}) };
}

export class Collector {
  constructor(file, notify) {
    this.file = file;
    this.notify = notify;
    this.sources = new Map();
    this.dismissed = new Map();
    this.pairings = new Map();
  }

  async load(config, legacy = [], dismissed = new Map()) {
    this.configure(config);
    try {
      const saved = JSON.parse(await readFile(this.file, 'utf8'));
      if (saved.version !== 1 || !Array.isArray(saved.sources) || !Array.isArray(saved.dismissed)) {
        throw new Error('Invalid collector store');
      }
      for (const source of saved.sources) {
        if (source.installationId !== null || source.generation !== 0 || source.bootId !== null) {
          validateConnect({ version: 1, reporterId: source.id, installationId: source.installationId,
            bootId: source.bootId, generation: source.generation });
        }
        validateReport({ version: 1, reporterId: source.id, lease: '0'.repeat(64), seq: 1,
          sentAt: new Date(0).toISOString(), healthy: false, issues: [], members: source.members,
          relatives: source.relatives, notices: [] });
        if (this.sources.has(source.id)) throw new Error('Duplicate stored reporter');
        this.sources.set(source.id, { ...source, lease: null, healthy: false, armed: new Map(), activeRuns: new Map(),
          issues: ['Collector restarted; waiting for a fresh watcher baseline'] });
      }
      for (const [id, key] of saved.dismissed) {
        if (typeof id !== 'string' || !/^[a-f0-9]{64}$/.test(key)) throw new Error('Invalid collector dismissal');
        this.dismissed.set(id, key);
      }
    } catch (error) {
      if (error.code !== 'ENOENT') throw error;
      const local = config.reporters.find(row => row.legacy);
      if (legacy.length && !local) throw new Error('Legacy state has no local reporter');
      if (local && legacy.length) {
        this.sources.set(local.id, { id: local.id, label: local.label, installationId: null, generation: 0,
          bootId: null, lastSeen: null, ...metadata({ members: legacy }),
          healthy: false, lease: null, armed: new Map(), activeRuns: new Map(), issues: ['Waiting for local watcher after migration'] });
        for (const [id, key] of dismissed) this.dismissed.set(namespace(local.id, id), key);
      }
      await this.save();
    }
  }

  configure(config) { this.pairings = new Map(config.reporters.map(row => [row.id, row])); }
  async save() {
    const sources = [...this.sources.values()].map(source => Object.fromEntries(
      ['id', 'label', 'installationId', 'generation', 'bootId', 'lastSeen', 'members', 'relatives'].map(key => [key, source[key]])));
    await writeFile(`${this.file}.tmp`, JSON.stringify({ version: 1, sources, dismissed: [...this.dismissed] }), { mode: 0o600 });
    await rename(`${this.file}.tmp`, this.file);
  }

  live(source, now) {
    const age = now - source.lastSeen;
    return Boolean(this.pairings.has(source.id) && source.lease && source.healthy && age >= 0 && age < HEARTBEAT_MS);
  }

  async connect(value, now = Date.now()) {
    validateConnect(value);
    const pairing = this.pairings.get(value.reporterId);
    if (!pairing) throw fail('Reporter not paired', 403);
    let source = this.sources.get(value.reporterId);
    if (source?.installationId && source.installationId !== value.installationId) throw fail('Reporter installation identity conflict', 409);
    if (source && (value.generation < source.generation ||
        value.generation === source.generation && source.bootId !== value.bootId)) throw fail('Old watcher generation', 409);
    if (source?.lease && source.bootId !== value.bootId && now - source.lastSeen >= 0 && now - source.lastSeen < HEARTBEAT_MS) {
      throw fail('Another watcher instance holds this identity', 409);
    }
    source = { ...source, id: pairing.id, label: pairing.label, installationId: value.installationId,
      generation: value.generation, bootId: value.bootId, lease: randomBytes(32).toString('hex'),
      seq: 0, bodyHash: null, lastSeen: now, healthy: false, armed: new Map(), activeRuns: new Map(),
      members: source?.members ?? [], relatives: source?.relatives ?? [], issues: ['Awaiting watcher baseline'] };
    const previous = this.sources.get(source.id);
    this.sources.set(source.id, source);
    try { await this.save(); }
    catch (error) {
      if (previous) this.sources.set(source.id, previous);
      else this.sources.delete(source.id);
      throw error;
    }
    return { version: 1, lease: source.lease, heartbeatMs: HEARTBEAT_MS, serverTime: new Date(now).toISOString() };
  }

  async accept(value, now = Date.now()) {
    validateReport(value);
    const source = this.sources.get(value.reporterId);
    if (!source || !this.pairings.has(source.id) || source.lease !== value.lease ||
        now - source.lastSeen < 0 || now - source.lastSeen >= HEARTBEAT_MS) throw fail('Lease expired; reconnect and baseline', 409);
    const hash = digest(JSON.stringify(value));
    if (value.seq === source.seq && hash === source.bodyHash) return { seq: value.seq, duplicate: true };
    if (value.seq !== source.seq + 1) throw fail('Out-of-order or conflicting report', 409);
    const baseline = source.seq === 0 || !source.healthy;
    const before = structuredClone(source), dismissedBefore = new Map(this.dismissed);
    const skew = Math.abs(now - Date.parse(value.sentAt)) > 30000 || value.members.some(row =>
      [row.firstObservedAt, row.startedAt, row.lastEventAt, row.lastResponseAt, row.finishedAt, row.lastAlert?.at]
        .some(at => at && Date.parse(at) > now + 30000));
    const previous = new Map(source.members.map(row => [row.id, row]));
    const rows = value.members.map(row => {
      const saved = previous.get(row.id);
      previous.delete(row.id);
      const lastAlert = saved?.lastAlert && (!row.lastAlert ||
        Date.parse(saved.lastAlert.at) >= Date.parse(row.lastAlert.at)) ? saved.lastAlert : row.lastAlert;
      const retainedFinish = saved?.state === 'finished' && saved.runId === row.runId &&
        saved.startedAt === row.startedAt && saved.finishedAt === row.finishedAt;
      const observedFinish = !baseline && value.healthy && !skew && source.activeRuns.get(row.id) === row.runId;
      const member = { ...row, firstObservedAt: saved?.firstObservedAt ?? row.firstObservedAt, lastAlert };
      if (row.state === 'finished' && !retainedFinish && !observedFinish) {
        return unknown(member, 'Completion occurred outside continuous collector observation; not confirmed');
      }
      return member;
    });
    // A member already confirmed 'finished' is a terminal, settled fact; the
    // watcher's reporting window can legitimately narrow to exclude a long-
    // quiet session without that session's completion becoming any less
    // true. Only non-terminal (still-open) omissions represent a genuine
    // loss of tracking and need to be surfaced as unconfirmed. This trust
    // only applies to continuous, already-healthy reporting: a baseline
    // report (fresh connect, or recovering from unhealthy - e.g. right
    // after a migration from a single-machine install) carries no live
    // guarantee about rows it didn't just observe, so those must still
    // surface as unconfirmed rather than silently resuming a stale dismissal.
    let trackingLost = 0;
    for (const row of previous.values()) {
      if (!baseline && row.state === 'finished') { rows.push(row); continue; }
      trackingLost++;
      rows.push(unknown(row, 'Watcher omitted a retained member; completion is unconfirmed'));
    }
    if (rows.length > 5000) throw fail('Retained member limit exceeded', 413);
    source.seq = value.seq;
    source.bodyHash = hash;
    source.lastSeen = now;
    source.members = rows;
    source.relatives = value.relatives;
    source.healthy = value.healthy && !skew;
    source.issues = [...value.issues, ...(skew ? ['Watcher clock differs by more than 30 seconds'] : []),
      ...(trackingLost ? ['Watcher omitted retained members; omitted states are unconfirmed'] : [])];
    if (!source.healthy) { source.armed.clear(); source.activeRuns.clear(); }
    else for (const row of rows) {
      if (row.state === 'working' && row.completionTracked) source.activeRuns.set(row.id, row.runId);
      else if (!row.completionTracked) source.activeRuns.delete(row.id);
    }
    const notices = [];
    for (const family of groupFamilies(rows, source.relatives)) {
      const familyId = namespace(source.id, family.id);
      if (family.state === 'working' || family.dismissKey && this.dismissed.get(familyId) !== family.dismissKey) {
        this.dismissed.delete(familyId);
      }
      const invalid = !source.healthy || family.hierarchyIssue ||
        family.members.some(row => row.state === 'unknown' && !row.completionTracked);
      if (invalid) source.armed.delete(family.id);
      const events = value.notices.filter(notice => notice.familyId === family.id);
      if (source.healthy && !baseline) {
        for (const event of events) {
          if (event.kind === 'finished' && (family.state !== 'finished' ||
              source.armed.get(family.id) !== membership(family))) continue;
          if (event.kind === 'waiting' && family.state !== 'waiting' ||
              event.kind === 'error' && !family.members.some(row => row.state === 'error')) continue;
          const key = this.pairings.get(source.id).legacy ? event.key : digest(`${source.id}:${event.key}`);
          notices.push({ key, kind: event.kind, title: `${source.label}: ${family.title}`,
            message: event.kind === 'finished' ? 'All observed family runs finished; not task or PR success.'
              : event.kind === 'waiting' ? 'Family needs input or approval; the run is not finished.'
                : event.kind === 'error' ? 'Family has an error or interruption; not successful completion.'
                  : 'Family status is unconfirmed; no completion inferred.' });
        }
      }
      if (!invalid && family.state === 'working') source.armed.set(family.id, membership(family));
      else if (family.state === 'finished') source.armed.delete(family.id);
    }
    try { await this.save(); }
    catch (error) {
      this.sources.set(source.id, before);
      this.dismissed = dismissedBefore;
      throw error;
    }
    for (const { key, ...alert } of notices) await this.notify(key, alert);
    return { seq: source.seq, healthy: source.healthy };
  }

  disconnect(id, lease) {
    const source = this.sources.get(id);
    if (!source || source.lease !== lease) throw fail('Invalid watcher lease', 409);
    source.lease = null;
    source.healthy = false;
    source.armed.clear();
    source.activeRuns.clear();
    source.issues = ['Watcher stopped or disconnected'];
    return { disconnected: true };
  }

  snapshot(now = Date.now(), includeHidden = false) {
    const sessions = [], members = [], sources = [];
    for (const pairing of this.pairings.values()) {
      if (!this.sources.has(pairing.id)) sources.push({ id: pairing.id, label: pairing.label,
        healthy: false, lastSeen: null, issues: ['Paired; watcher has not connected'] });
    }
    for (const source of this.sources.values()) {
      const online = this.live(source, now);
      if (!online) { source.armed.clear(); source.activeRuns.clear(); }
      const reason = !this.pairings.has(source.id) ? 'Reporter pairing revoked'
        : !source.healthy ? source.issues.join('; ') || 'Watcher unavailable'
          : 'Watcher heartbeat expired; source status is unconfirmed';
      const rows = online ? source.members : source.members.map(row => unknown(row, reason));
      sources.push({ id: source.id, label: source.label, healthy: online,
        lastSeen: source.lastSeen ? new Date(source.lastSeen).toISOString() : null,
        issues: online ? source.issues : [reason] });
      members.push(...rows.map(row => mapped(row, source)));
      for (const family of groupFamilies(rows, source.relatives)) {
        const row = mapped(family, source);
        if (includeHidden || !row.dismissKey || this.dismissed.get(row.id) !== row.dismissKey) sessions.push(row);
      }
    }
    const ordered = sortSessions(sessions);
    return { sessions: ordered, members: sortSessions(members), sources,
      active: ordered.filter(row => row.state === 'working'),
      attention: ordered.filter(row => ['waiting', 'error', 'unknown'].includes(row.state)),
      issues: sources.flatMap(source => source.issues.map(issue => `${source.label} (${source.id.slice(0, 8)}): ${issue}`)),
      coverage: `${sources.filter(source => source.healthy).length}/${sources.length} paired Windows watchers connected` };
  }

  dismiss(entries, now = Date.now()) {
    if (!Array.isArray(entries) || !entries.length || entries.length > 1000 || entries.some(entry =>
      typeof entry?.id !== 'string' || entry.id.length > 200 || !/^[a-f0-9]{64}$/.test(entry.key))) {
      throw new TypeError('Expected 1-1000 finished family IDs and revision keys');
    }
    const current = new Map(this.snapshot(now, true).sessions.map(row => [row.id, row]));
    const result = { dismissed: [], skipped: [] };
    for (const entry of entries) {
      if (!current.get(entry.id)?.dismissKey || current.get(entry.id).dismissKey !== entry.key) {
        result.skipped.push({ id: entry.id, reason: 'No longer the same connected, safely finished family' });
      } else {
        this.dismissed.set(entry.id, entry.key);
        result.dismissed.push(entry.id);
      }
    }
    return result;
  }
}
