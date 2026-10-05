const $ = id => document.getElementById(id);
let controlToken;
let state;
let stopped = false;
let dismissing = false;
let statusRequest = 0;
const cards = new Map();

function elapsed(start) {
  if (!start) return 'Start time unavailable';
  const seconds = Math.max(0, Math.floor((Date.now() - Date.parse(start)) / 1000));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  return hours ? `${hours}h ${minutes}m elapsed` : minutes ? `${minutes}m ${seconds % 60}s elapsed` : `${seconds}s elapsed`;
}

function time(value, compact = false) {
  return value ? new Date(value).toLocaleString(undefined, {
    dateStyle: compact ? 'short' : 'medium', timeStyle: compact ? 'short' : 'medium',
  }) : 'unavailable';
}

function applyTheme(theme) {
  if (theme?.source === 'windows-apps' && ['light', 'dark'].includes(theme.mode)) {
    document.documentElement.dataset.theme = theme.mode;
    $('theme').textContent = `Theme: Windows app preference (${theme.mode}) - follows Windows changes automatically`;
  } else {
    delete document.documentElement.dataset.theme;
    $('theme').textContent = `Theme: browser fallback. ${theme?.reason ?? 'Windows preference unavailable'}`;
  }
}

function unavailable(rows, reason) {
  return rows.map(item => {
    const result = item.state !== 'unobserved'
      ? { ...item, state: 'unknown', detail: reason, finishedAt: null, dismissKey: null } : { ...item };
    if (item.members) {
      result.members = unavailable(item.members, reason);
      if (item.relatives) result.relatives = unavailable(item.relatives, reason);
      result.runningCount = 0;
    }
    return result;
  });
}

function stateLabel(item) {
  return {
    working: 'Working', finished: 'Run finished', unknown: 'Unconfirmed', idle: 'Idle (no run observed)',
    unobserved: 'Not observed',
    error: item.detail === 'Run interrupted' ? 'Interrupted' : 'Error',
    waiting: item.members ? 'Needs input' : item.detail === 'Input needed' ? 'Waiting for input' : item.detail,
  }[item.state] ?? 'Unconfirmed';
}

function row(item) {
  let parts = cards.get(item.id);
  if (!parts) {
    const element = (tag, className) => {
      const node = document.createElement(tag);
      node.className = className;
      return node;
    };
    parts = {
      card: element('details', 'card'), summary: element('summary', 'row-summary'),
      title: element('h3', 'row-title'), badge: element('span', 'badge'),
      compactTime: element('p', 'compact-time'), icon: element('span', 'disclosure-icon'),
      body: element('div', 'row-details'), fullTitle: element('p', 'full-title'),
      metadata: element('p', 'muted'), detail: element('p', 'detail'),
      response: element('p', 'timestamp'), finished: element('p', 'timestamp finished-time'),
      identity: element('small', 'identity'), parentAlert: element('p', 'parent-alert'),
      relatives: element('div', 'relatives'), hierarchy: element('p', 'hierarchy-warning'),
      dismiss: element('button', 'dismiss'),
    };
    parts.dismiss.type = 'button';
    parts.dismiss.textContent = 'Dismiss';
    parts.dismiss.addEventListener('click', event => {
      event.preventDefault();
      event.stopPropagation();
      return dismissEntries([{ id: parts.item.id, key: parts.item.dismissKey }]);
    });
    parts.icon.textContent = '+';
    parts.icon.setAttribute('aria-hidden', 'true');
    parts.summary.append(parts.title, parts.badge, parts.compactTime, parts.icon, parts.hierarchy, parts.dismiss);
    parts.body.append(parts.fullTitle, parts.metadata, parts.detail, parts.response, parts.finished,
      parts.parentAlert, parts.relatives, parts.identity);
    parts.card.append(parts.summary, parts.body);
    cards.set(item.id, parts);
  }
  const { card, title, badge, compactTime, fullTitle, metadata, detail, response, finished, identity,
    parentAlert, relatives, hierarchy, dismiss } = parts;
  parts.item = item;
  dismiss.hidden = !['finished', 'unknown'].includes(item.state) || !item.dismissKey;
  dismiss.disabled = !state.healthy || dismissing;
  dismiss.setAttribute('aria-label', `Dismiss ${item.title} from this monitor only`);
  dismiss.title = 'Remove this finished or unconfirmed entry from this monitor only. Copilot sessions and files are unchanged.';
  const text = (node, value) => {
    if (node.textContent !== value) node.textContent = value;
  };
  card.className = `card ${item.state}`;
  const machineLabel = item.machineTag ? `[${item.machine.length > 16 ? `${item.machine.slice(0, 15)}...` : item.machine} /${item.reporterId.slice(0, 8)}] ` : '';
  text(title, `${machineLabel}${item.title}`);
  title.title = item.machineTag ? `${item.machineTag} | ${item.title}` : item.title;
  text(fullTitle, item.title);
  text(badge, `${stateLabel(item)}${item.members && item.state === 'working' && item.childCount
    ? ` ${item.runningCount}/${item.members.length}` : ''}`);
  badge.title = item.members ? `${item.runningCount} working, ${item.childCount} observed descendants` : '';
  const hasFinishedTime = item.state === 'finished' && item.finishedAt;
  let compact = hasFinishedTime ? `Finished (observed): ${time(item.finishedAt, true)}`
    : item.lastResponseAt ? `Response: ${time(item.lastResponseAt, true)}`
      : `First seen: ${time(item.firstObservedAt, true)} (no response)`;
  if (item.members) {
    const alert = item.parentAlert;
    const label = alert && { finished: 'Finished', waiting: 'Needs input', error: 'Error', warning: 'Unconfirmed' }[alert.kind];
    compact = alert ? `Parent alert: ${label} - ${time(alert.at, true)}`
      : hasFinishedTime ? `No parent alert; family finished ${time(item.finishedAt, true)}`
        : `No parent alert observed; ${item.lastResponseAt ? 'response' : 'first seen'} ${time(item.lastResponseAt ?? item.firstObservedAt, true)}`;
    text(parentAlert, alert ? `Parent's own monitor alert: ${label} at ${time(alert.at)}. ${alert.message}`
      : 'No parent monitor alert observed. Child alerts are not substituted; historical alerts are not reconstructed.');
    const details = item.relatives ?? item.members.map(member => ({ ...member, depth: member.id === item.id ? 0 : 1 }));
    const signature = JSON.stringify(details);
    if (parts.relatedSignature !== signature) {
      for (const child of [...relatives.children]) child.remove();
      for (const member of details) {
        const child = document.createElement('p');
        child.className = `relative depth-${Math.min(member.depth, 3)}`;
        const role = member.id === item.id ? 'Parent' : member.aliasOf ? 'Previous runtime'
          : member.depth === 1 ? 'Child' : `Descendant (level ${member.depth})`;
        child.textContent = `${role}: ${member.title} - ${stateLabel(member)}. ${member.detail}${member.hierarchyIssue ? `; ${member.hierarchyIssue}` : ''}`;
        relatives.append(child);
      }
      parts.relatedSignature = signature;
    }
  }
  parentAlert.hidden = !item.members;
  relatives.hidden = !item.members;
  hierarchy.hidden = !item.hierarchyIssue;
  text(hierarchy, item.hierarchyIssue ? `Hierarchy unconfirmed: ${item.hierarchyIssue}` : '');
  text(compactTime, compact);
  compactTime.className = `compact-time${hasFinishedTime ? ' finished-time' : ''}`;
  text(metadata, `${item.source} | ${item.machineTag ?? item.machine}`);
  text(detail, item.state === 'working' && !item.members ? `${item.activity} | ${elapsed(item.startedAt)}` : item.detail);
  text(response, item.lastResponseAt ? `Last assistant response: ${time(item.lastResponseAt)}`
    : `No assistant response recorded. First observed: ${time(item.firstObservedAt)} (ordering fallback)`);
  finished.hidden = !hasFinishedTime;
  text(finished, hasFinishedTime ? `${item.members ? 'Family' : 'Run'} finished (observed): ${time(item.finishedAt)}` : '');
  text(identity, `Session ${item.id}`);
  return card;
}

function render() {
  if (!state) return;
  applyTheme(state.theme);
  const sessions = state.healthy ? state.sessions : unavailable(state.sessions, 'Observer disconnected; current run status is unconfirmed');
  const running = sessions.filter(item => item.state === 'working');
  const retained = sessions.filter(item => item.state !== 'working');
  const working = running.length;
  $('count').textContent = sessions.length;
  $('summary').textContent = `${working} working | ${sessions.length} retained ${state.members ? 'families' : 'sessions'}`;
  $('clear-finished').disabled = !state.healthy || dismissing ||
    !sessions.some(item => item.state === 'finished' && item.dismissKey);
  $('coverage').textContent = `${state.healthy ? state.coverage : 'Live source coverage unavailable'} | ${state.machine} | Read-only observation`;
  $('source-health').hidden = !state.sources;
  if (state.sources) {
    const sources = state.sources.map(source => state.healthy ? source
      : { ...source, healthy: false, issues: ['Collector disconnected; cached source status is unconfirmed'] });
    $('source-summary').textContent = `${sources.filter(source => source.healthy).length}/${sources.length} paired sources connected (expand coverage)`;
    $('source-details').textContent = sources.map(source =>
      `${source.label} (${source.id.slice(0, 8)}): ${source.healthy ? 'Connected' : 'UNAVAILABLE / Unconfirmed'}; last received: ${time(source.lastSeen)}${source.issues.length ? `. ${source.issues.join('; ')}` : ''}`)
      .join('\n') || 'No watchers paired. This is not evidence that other machines are idle.';
  }
  $('health').textContent = state.healthy
    ? (state.issues.length ? `Observing with limitations: ${state.issues.join('; ')}` : 'Live local observer connected')
    : `Status unavailable - no completion inferred. ${state.issues.join('; ')}`;
  $('health').className = state.healthy && !state.issues.length ? 'connected' : 'warning';
  const focused = document.activeElement;
  for (const [id, items] of [['running', running], ['retained', retained]]) {
    const list = $(id);
    const rows = items.map(item => row(item));
    for (const child of [...list.children]) {
      if (!rows.includes(child)) child.remove();
    }
    rows.forEach((card, index) => {
      if (list.children[index] !== card) list.insertBefore(card, list.children[index] ?? null);
    });
    $(`${id}-count`).textContent = items.length;
    $(`${id}-empty`).hidden = items.length > 0;
  }
  for (const id of cards.keys()) {
    if (!sessions.some(item => item.id === id)) cards.delete(id);
  }
  if (focused?.isConnected && focused.hidden && focused.className === 'dismiss') {
    [...cards.values()].find(parts => parts.dismiss === focused)?.summary.focus({ preventScroll: true });
  } else if (focused?.isConnected && document.activeElement !== focused) focused.focus({ preventScroll: true });
  $('running-empty').textContent = state.healthy
    ? 'No sessions currently observed running.' : 'Running status unavailable; see Unconfirmed rows.';
  const n = state.notification;
  $('notification').textContent = n.title
    ? `${n.title} - ${n.state === 'shown' ? 'Windows reported the notification shown' : n.state}. ${n.message}`
    : n.message;
  $('updated').textContent = state.updatedAt ? `Last checked ${new Date(state.updatedAt).toLocaleTimeString()}. No transcript content is displayed or sent.` : '';
  document.title = `${working} working, ${sessions.length} retained - Copilot session monitor`;
}

async function refresh() {
  if (stopped) return;
  await loadStatus();
  if (!stopped) setTimeout(refresh, 1500);
}

async function loadStatus() {
  const request = ++statusRequest;
  try {
    const response = await fetch('/api/status', { signal: AbortSignal.timeout(5000) });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const next = await response.json();
    if (request !== statusRequest) return;
    state = next;
    render();
  } catch {
    if (request !== statusRequest) return;
    if (state) {
      state = { ...state, healthy: false, theme: null };
      render();
    }
    $('health').textContent = 'Monitor disconnected. Run Start-Monitor.ps1 to reconnect. No completion inferred.';
    $('health').className = 'warning';
    document.title = 'Status unavailable - Copilot session monitor';
  }
}

async function control(action, body) {
  const response = await fetch('/api/control', { signal: AbortSignal.timeout(5000) });
  if (!response.ok) throw new Error('Cannot connect to monitor controls');
  controlToken = (await response.json()).token;
  const result = await fetch(`/api/${action}`, {
    body: body === undefined ? undefined : JSON.stringify(body),
    method: 'POST', headers: { Authorization: `Bearer ${controlToken}` }, signal: AbortSignal.timeout(5000),
  });
  if (!result.ok) {
    const error = await result.json();
    throw new Error(error.error ?? `Monitor control failed (${result.status})`);
  }
  return result.json();
}

async function dismissEntries(entries) {
  if (dismissing) return;
  dismissing = true;
  render();
  const resultNode = $('dismiss-result');
  resultNode.hidden = false;
  resultNode.textContent = 'Checking current state before dismissing monitor entries...';
  try {
    const result = await control('dismiss', { entries });
    await loadStatus();
    resultNode.textContent = `Dismissed ${result.dismissed.length} from this monitor only. Copilot sessions and files are unchanged.${result.skipped.length ? ` Skipped ${result.skipped.length}: no longer the same safely finished entries.` : ''}`;
  } catch (error) {
    resultNode.textContent = `Dismiss failed: ${error.message}`;
  } finally {
    dismissing = false;
    render();
    resultNode.focus({ preventScroll: true });
  }
}

$('clear-finished').addEventListener('click', () => dismissEntries(state.sessions
  .filter(item => item.state === 'finished' && item.dismissKey).map(item => ({ id: item.id, key: item.dismissKey }))));

$('test').addEventListener('click', async () => {
  $('test').disabled = true;
  try {
    await control('test');
    $('notification').textContent = 'Test queued in the Windows tray. Check your desktop notifications.';
  } catch (error) {
    $('notification').textContent = error.message;
  } finally {
    setTimeout(() => { $('test').disabled = false; }, 5000);
  }
});
$('stop').addEventListener('click', async () => {
  try {
    await control('stop');
    stopped = true;
    if (state) {
      state = { ...state, healthy: false, theme: null };
      render();
    }
    $('health').textContent = 'Collector stopped. Watchers may keep retrying; Stop-Monitor.ps1 stops both local roles. Copilot sessions were not changed.';
    $('health').className = 'warning';
    $('stop').disabled = true;
    document.title = 'Stopped - Copilot session monitor';
  } catch (error) {
    $('health').textContent = error.message;
  }
});
refresh();
