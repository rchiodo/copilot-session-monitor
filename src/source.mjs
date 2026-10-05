import { DatabaseSync } from 'node:sqlite';
import { readdir, stat } from 'node:fs/promises';
import path from 'node:path';
import { JsonlTail } from './events.mjs';
import { hierarchyIndex, selectedHierarchy, relatedMetadata } from './hierarchy.mjs';

export const SESSION_ID = /^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/i;

function activity(events, owner) {
  const current = at => at && Date.parse(at) >= Date.parse(owner.startedAt);
  const background = events.backgroundCount > 0;
  const foreground = (events.activeTurn || events.activeTools > 0) && !events.terminal;
  return {
    busy: !events.closed && (background && current(events.backgroundAt) ||
      foreground && current(events.lastExecutionAt)),
    activityUnconfirmed: events.backgroundUnconfirmed || background && !current(events.backgroundAt)
      ? 'Outstanding background work lacks confirmed current ownership or lifecycle evidence' : null,
    completionUnconfirmed: events.terminal && !current(events.terminal.at)
      ? 'Completion evidence predates the current process owner' : null,
  };
}

export function desktopRows(file) {
  const db = new DatabaseSync(file, { readOnly: true });
  try {
    // Read only named metadata columns; never fetch credentials or transcripts.
    db.exec('BEGIN');
    const sessions = db.prepare(`
      SELECT s.id, s.title, s.is_running, s.was_interrupted,
        s.execution_location, s.session_type, s.archived_at,
        (SELECT w.host_id FROM workspaces w WHERE w.session_id = s.id LIMIT 1) AS host_id
      FROM sessions s
    `).all();
    const nodes = hierarchyIndex({
      sessions,
      workspaces: db.prepare('SELECT id,session_id,creator_session_id,host_id,archived_at FROM workspaces').all(),
      links: db.prepare('SELECT child_workspace_id,parent_workspace_id FROM workspace_parent_links').all(),
      aliases: db.prepare('SELECT session_id,workspace_id FROM workspace_session_aliases').all(),
      workspaceChats: db.prepare('SELECT workspace_id,session_id FROM workspace_side_chats').all(),
      sessionChats: db.prepare('SELECT parent_session_id,session_id FROM session_side_chats').all(),
    });
    db.exec('COMMIT');
    return [...nodes.values()];
  } finally {
    db.close();
  }
}

export class LocalSource {
  constructor(home, processSnapshot) {
    this.home = home;
    this.root = path.join(home, 'session-state');
    this.processSnapshot = processSnapshot;
    this.tails = new Map();
    this.liveIds = new Set();
    this.tracked = new Set();
    this.directoryIds = [];
    this.discoveredAt = 0;
  }

  async owner(id, processes, desktop) {
    const dir = path.join(this.root, id);
    const entries = await readdir(dir);
    const matches = [];
    for (const name of entries) {
      const match = /^inuse\.(\d+)\.lock$/.exec(name);
      if (!match) continue;
      const processInfo = processes.find(p => p.pid === Number(match[1]) && p.name === 'copilot.exe');
      if (!processInfo) continue;
      try { process.kill(processInfo.pid, 0); } catch { continue; }
      const lock = await stat(path.join(dir, name));
      if (Date.parse(processInfo.startedAt) > lock.mtimeMs + 2000) continue;
      const parent = processes.find(p => p.pid === processInfo.parentPid && p.name === 'github.exe');
      if (desktop && (!parent || Date.parse(parent.startedAt) > Date.parse(processInfo.startedAt))) continue;
      if (desktop) {
        try { process.kill(parent.pid, 0); } catch { continue; }
      }
      if (!desktop && parent) continue;
      matches.push({
        key: `${processInfo.pid}:${processInfo.startedAt}:${parent?.startedAt ?? 'cli'}`,
        startedAt: processInfo.startedAt,
      });
    }
    if (matches.length > 1) throw new Error('Multiple live session owners');
    return matches[0] ?? null;
  }

  async poll() {
    const processState = this.processSnapshot();
    if (!processState || Date.now() - processState.at > 8000 || processState.error) {
      throw new Error('Windows process observer unavailable');
    }
    const rows = desktopRows(path.join(this.home, 'data.db'));
    const knownIds = new Set(rows.map(row => row.id));
    const nodes = new Map(rows.map(row => [row.id, row]));
    const samples = [];
    const issues = [];
    const reads = new Map();
    const discovered = new Set(this.tracked);
    this.liveIds.clear();
    const read = async row => {
      const owner = await this.owner(row.id, processState.processes, true);
      if (!owner) return { owner: null, events: null };
      this.liveIds.add(row.id);
      const tail = this.tails.get(row.id) ?? new JsonlTail(path.join(this.root, row.id, 'events.jsonl'));
      this.tails.set(row.id, tail);
      const events = await tail.read();
      return { owner, events, ...activity(events, owner) };
    };
    // A foreground-idle session can still own an attached command or background agent.
    // Probe live local owners, but retain only actual work/uncertainty, not idle history.
    for (const row of rows) {
      if (this.tracked.has(row.id) || row.is_running || row.archived_at ||
          row.execution_location !== 'local' || row.host_id && row.host_id !== 'local' || !SESSION_ID.test(row.id)) continue;
      try {
        const result = await read(row);
        reads.set(row.id, result);
        if (result.busy || result.activityUnconfirmed) discovered.add(row.id);
      } catch (error) {
        if (error.code !== 'ENOENT' || this.liveIds.has(row.id)) {
          issues.push(`${row.title}: activity discovery unavailable (${error.code ?? error.name})`);
          reads.set(row.id, { readError: `Activity discovery unavailable (${error.code ?? error.name})` });
          if (this.liveIds.has(row.id)) discovered.add(row.id);
        }
      }
    }
    const local = selectedHierarchy(nodes, discovered);
    for (const row of local) {
      const sample = {
        id: row.id, title: row.title || `Name unavailable (${row.id.slice(0, 8)})`,
        source: 'Copilot desktop', busy: row.is_running === 1,
        interrupted: row.was_interrupted === 1, alive: false, owner: null, events: null,
        parentId: row.parentId, hierarchyIssue: row.hierarchyIssue,
        contextOnly: !discovered.has(row.id) && !row.is_running,
      };
      if (!SESSION_ID.test(row.id) || !row.local || row.archived_at) {
        sample.readError = row.hierarchyIssue ?? (row.archived_at
          ? 'Related app session is archived; current status unavailable' : 'Related session is outside local coverage');
        sample.hierarchyIssue = sample.readError;
        sample.contextOnly = true;
        issues.push(`${sample.title}: ${sample.readError}`);
        samples.push(sample);
        continue;
      }
      if (row.hierarchyIssue) issues.push(`${sample.title}: ${row.hierarchyIssue}`);
      try {
        const result = reads.get(row.id) ?? await read(row);
        const { owner } = result;
        sample.readError = result.readError;
        if (owner) {
          sample.alive = true;
          sample.owner = owner.key;
          sample.events = result.events;
          sample.busy ||= Boolean(result.busy);
          sample.activityUnconfirmed = result.activityUnconfirmed;
          sample.completionUnconfirmed = result.completionUnconfirmed;
          if (sample.busy && (!sample.events.runId || sample.events.closed)) {
            issues.push(`${sample.title}: running flag lacks current execution evidence; not counted as working`);
          }
          if (sample.busy || sample.activityUnconfirmed) this.tracked.add(row.id);
        } else if (sample.busy) {
          issues.push(`${sample.title}: running flag has no live local owner; not counted as working`);
        }
      } catch (error) {
        sample.readError = `Session reader unavailable (${error.code ?? error.name})`;
        issues.push(`${sample.title}: ${sample.readError}`);
      }
      samples.push(sample);
    }
    if (Date.now() - this.discoveredAt > 5000) {
      this.directoryIds = (await readdir(this.root, { withFileTypes: true }))
        .filter(entry => entry.isDirectory() && SESSION_ID.test(entry.name)).map(entry => entry.name);
      this.discoveredAt = Date.now();
    }
    for (const id of this.directoryIds) {
      if (knownIds.has(id)) continue;
      try {
        const owner = await this.owner(id, processState.processes, false);
        if (!owner) continue;
        const tail = this.tails.get(id) ?? new JsonlTail(path.join(this.root, id, 'events.jsonl'));
        this.tails.set(id, tail);
        const events = await tail.read();
        samples.push({
          id, title: events.cwd ? `${path.win32.basename(events.cwd)} - CLI ${id.slice(0, 8)}` : `CLI ${id.slice(0, 8)}`,
          source: 'CLI (activity only)',
          busy: events.activeTurn, interrupted: false, alive: true, owner: owner.key, events,
        });
      } catch (error) {
        if (error.code !== 'ENOENT') issues.push(`CLI ${id.slice(0, 8)}: reader unavailable (${error.code ?? error.name})`);
      }
    }
    return { samples, issues, relatives: relatedMetadata(nodes, local),
      desktopSessions: rows.filter(row => row.execution_location === 'local' && !row.archived_at).length };
  }

  releaseIdle(keepIds) {
    for (const id of this.tracked) {
      if (!keepIds.has(id)) {
        this.tracked.delete(id);
        this.tails.delete(id);
      }
    }
    for (const id of this.tails.keys()) {
      if (!keepIds.has(id) && !this.tracked.has(id) && !this.liveIds.has(id)) this.tails.delete(id);
    }
  }
}
