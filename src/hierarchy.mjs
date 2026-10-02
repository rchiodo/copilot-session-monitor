export function hierarchyIndex({ sessions, workspaces, links, aliases, workspaceChats, sessionChats }) {
  const nodes = new Map(sessions.map(row => [row.id, { ...row, parentId: null, hierarchyIssue: null }]));
  const ensure = id => {
    if (id && !nodes.has(id)) nodes.set(id, { id, title: `Unavailable session ${id.slice(0, 8)}`,
      parentId: null, hierarchyIssue: 'Session metadata unavailable', execution_location: null, is_running: 0 });
  };
  for (const row of workspaces) ensure(row.session_id);
  for (const row of [...workspaceChats, ...sessionChats]) ensure(row.session_id);
  const workspaceIds = new Map(workspaces.map(row => [row.id, row]));
  const references = new Map();
  const bind = (reference, id) => {
    if (!reference || !id) return;
    const targets = references.get(reference) ?? new Set();
    targets.add(id);
    references.set(reference, targets);
  };
  for (const row of workspaces) {
    bind(row.id, row.session_id);
    bind(row.session_id, row.session_id);
  }
  for (const row of aliases) bind(row.session_id, workspaceIds.get(row.workspace_id)?.session_id);
  const resolve = reference => {
    const targets = references.get(reference);
    if (targets?.size > 1) return { id: reference, issue: 'Ambiguous app session identity' };
    const id = targets?.values().next().value ?? reference;
    return { id, issue: nodes.has(id) ? null : 'Recorded parent is missing' };
  };
  const parent = (childId, reference, issue = null) => {
    const child = nodes.get(childId);
    if (!child || !reference) return;
    const resolved = resolve(reference);
    if (child.parentId && child.parentId !== resolved.id) {
      child.hierarchyIssue = 'Conflicting recorded parents';
      return;
    }
    child.parentId = resolved.id;
    child.hierarchyIssue = issue ?? resolved.issue ?? child.hierarchyIssue;
    if (!nodes.has(resolved.id)) {
      nodes.set(resolved.id, {
        id: resolved.id, title: `Unavailable parent ${resolved.id.slice(0, 8)}`,
        parentId: null, hierarchyIssue: 'Recorded parent is missing',
        execution_location: null, is_running: 0,
      });
    }
  };
  for (const row of aliases) {
    const target = workspaceIds.get(row.workspace_id)?.session_id;
    if (target && row.session_id !== target) {
      parent(row.session_id, row.workspace_id);
      const node = nodes.get(row.session_id);
      if (node) node.aliasOf = target;
    }
  }
  for (const row of workspaces) {
    const node = nodes.get(row.session_id);
    if (!node) continue;
    node.host_id = row.host_id;
    if (row.archived_at) node.archived_at = row.archived_at;
    const recorded = links.filter(link => link.child_workspace_id === row.id);
    if (recorded.length) {
      for (const link of recorded) parent(row.session_id, link.parent_workspace_id);
    } else if (row.creator_session_id) {
      const creator = resolve(row.creator_session_id);
      const type = nodes.get(creator.id)?.session_type;
      parent(row.session_id, row.creator_session_id,
        type === 'project' ? 'Workspace parent link is missing' : creator.issue);
    }
  }
  for (const row of workspaceChats) parent(row.session_id, row.workspace_id);
  for (const row of sessionChats) parent(row.session_id, row.parent_session_id);
  return nodes;
}

export function rootOf(id, nodes) {
  const path = [];
  let current = id;
  while (nodes.get(current)?.parentId) {
    if (path.includes(current)) {
      return { id: path.slice(path.indexOf(current)).sort()[0], issue: 'Cycle in recorded app hierarchy' };
    }
    path.push(current);
    current = nodes.get(current).parentId;
  }
  return { id: current, issue: nodes.has(current) ? null : 'Recorded parent is missing' };
}

export function selectedHierarchy(nodes, tracked) {
  const local = row => row.execution_location === 'local' && (!row.host_id || row.host_id === 'local');
  const selected = new Set([...tracked].filter(id => nodes.has(id)));
  for (const row of nodes.values()) if (!row.archived_at && row.is_running && local(row)) selected.add(row.id);
  const roots = new Set([...selected].map(id => rootOf(id, nodes).id));
  // A known running relative outside this machine blocks family completion, not local coverage.
  for (const row of nodes.values()) if (row.is_running && roots.has(rootOf(row.id, nodes).id)) selected.add(row.id);
  for (const id of [...selected]) {
    let current = id;
    const visited = new Set();
    while (nodes.has(current) && !visited.has(current)) {
      selected.add(current);
      visited.add(current);
      current = nodes.get(current).parentId;
    }

  }
  return [...selected].map(id => {
    const row = nodes.get(id);
    return { ...row, hierarchyIssue: row.hierarchyIssue ?? rootOf(id, nodes).issue, local: local(row) };
  });
}

export function relatedMetadata(nodes, selected) {
  const roots = new Set(selected.map(row => rootOf(row.id, nodes).id));
  const observed = new Set(selected.map(row => row.id));
  return [...nodes.values()].filter(row => (!row.aliasOf || observed.has(row.id)) &&
    roots.has(rootOf(row.id, nodes).id)).map(row => ({
    id: row.id, parentId: row.parentId,
    aliasOf: row.aliasOf ?? null,
    title: row.title || `Name unavailable (${row.id.slice(0, 8)})`,
    hierarchyIssue: row.hierarchyIssue ?? rootOf(row.id, nodes).issue,
    detail: row.archived_at ? 'Archived; execution not observed'
      : row.execution_location !== 'local' || (row.host_id && row.host_id !== 'local')
        ? 'Unavailable locally; execution not observed' : 'Execution not observed',
  }));
}
