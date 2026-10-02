import http from 'node:http';
import { readFile } from 'node:fs/promises';
import { FamilyMonitor, groupFamilies } from '../src/families.mjs';
import { MonitorActions, readDismissEntries } from '../src/actions.mjs';

// Synthetic browser fixtures only; never reads Copilot data or sends monitor controls.
const timestamp = '2026-10-02T22:00:00.000Z';
const parents = Array.from({ length: 24 }, (_, index) => {
  const states = ['finished', 'waiting', 'error', 'unknown', 'finished', 'error', 'waiting', 'waiting',
    'finished', 'finished', 'finished', 'finished'];
  const state = index < 12 ? 'working' : states[index - 12];
  const titles = ['Notebook completion reliability', 'Review requested parser changes',
    `LongUnbrokenSessionTitle${'AndMoreTitle'.repeat(20)}`, 'Investigate environment selection'];
  const finishedAt = state === 'finished' ? timestamp : null;
  return {
    id: `fixture-${index}`, title: titles[index % titles.length], state,
    machine: `TEST-MACHINE-${'LongMachineName'.repeat(12)}`, source: 'Copilot desktop',
    startedAt: timestamp, firstObservedAt: timestamp,
    lastResponseAt: index === 8 ? null : new Date(Date.parse(timestamp) - index * 60_000).toISOString(),
    finishedAt, activity: 'Executing tools', parentId: null, contextOnly: false,
    lastAlert: index === 8 ? null : { sessionId: `fixture-${index}`, key: `alert-${index}`, at: timestamp,
      kind: index < 12 ? 'finished' : state === 'unknown' ? 'warning' : state,
      message: 'Parent monitor alert only; no transcript content' },
    detail: index === 17 ? 'Run interrupted' : index === 18 ? 'Permission needed' : index === 19 ? 'Plan approval needed'
      : state === 'waiting' ? 'Input needed' : state === 'finished' ? 'Current run finished; not task or PR success'
        : state === 'error' ? 'Run error' : state === 'unknown' ? 'Owner unavailable; no completion inferred' : 'Agent running',
  };
});
const members = parents.flatMap((parent, index) => [
  index < 12 ? { ...parent, state: 'finished', finishedAt: timestamp } : parent,
  { ...parent, id: `child-${index}`, parentId: parent.id, title: `Child ${index}`,
    lastAlert: { sessionId: `child-${index}`, key: `child-alert-${index}`, kind: 'error', at: timestamp, message: 'CHILD ALERT MUST NOT REPLACE PARENT' } },
]);
const relatives = parents.flatMap((parent, index) => [
  { id: `dormant-${index}`, parentId: `child-${index}`, title: `Dormant full child name ${'LongUnbrokenName'.repeat(16)}`, detail: 'Execution not observed' },
  { id: `missing-${index}`, parentId: `dormant-${index}`, title: 'Name unavailable (missing metadata)', detail: 'Metadata unavailable' },
]);
const sessions = groupFamilies(members, relatives);
const results = new Map();
const contexts = new Map();
function fixtureFor(request) {
  const params = new URL(request.headers.referer).searchParams;
  const id = params.get('fixture');
  if (!id) throw new Error('Fixture ID required');
  if (!contexts.has(id)) {
    const monitor = new FamilyMonitor('TEST', async () => {});
    monitor.engine.rows = new Map(members.map(row => [row.id, { ...row }]));
    monitor.relatives = relatives;
    contexts.set(id, { monitor, mode: params.get('mode'),
      actions: new MonitorActions(monitor, async () => {}, async () => {}, () => true) });
  }
  return contexts.get(id);
}
const expected = Object.fromEntries(['running', 'retained'].map(id =>
  [id, sessions.filter(row => (row.state === 'working') === (id === 'running')).map(row => row.title)]));

function measure() {
  const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
  const all = selector => [...document.querySelectorAll(selector)];
  const mode = new URL(location.href).searchParams.get('mode');
  const checks = {};
  const metrics = {};
  const check = (name, condition) => { checks[name] = Boolean(condition); };
  const within = () => document.documentElement.scrollWidth <= innerWidth
    && all('.card').every(card => card.getBoundingClientRect().right <= innerWidth);
  const run = async () => {
    for (let attempt = 0; !all('.card').length && attempt < 100; attempt++) await wait(100);
    if (all('.card').length !== 24) throw new Error('Expected 24 fixture rows');
    metrics.viewport = [innerWidth, innerHeight];
    const running = document.querySelector('#running');
    const retained = document.querySelector('#retained');
    const left = running.getBoundingClientRect();
    const right = retained.getBoundingClientRect();
    check('layout', innerWidth <= 900 ? right.top > left.bottom && Math.abs(right.left - left.left) < 1
      : right.left > left.right && Math.abs(right.top - left.top) < 1);
    check('theme', document.documentElement.dataset.theme === mode);
    check('palette', getComputedStyle(document.documentElement).backgroundColor ===
      (mode === 'dark' ? 'rgb(12, 18, 28)' : 'rgb(245, 247, 251)'));
    check('noOverflow', within());
    check('counts', document.querySelector('#running-count').textContent === '12'
      && document.querySelector('#retained-count').textContent === '12');
    for (const id of ['running', 'retained']) {
      const rows = all(`#${id} > .card`);
      metrics[id] = {
        firstRowTop: rows[0].getBoundingClientRect().top,
        heights: [...new Set(rows.map(row => row.getBoundingClientRect().height))],
        fullyVisible: rows.filter(row => {
          const box = row.getBoundingClientRect();
          return box.top >= 0 && box.bottom <= innerHeight;
        }).length,
      };
      check(`${id}Ordering`, JSON.stringify(rows.map(row => row.querySelector('.row-title').textContent)) ===
        JSON.stringify(window.expected[id]));
      check(`${id}Height`, rows.every(row => {
        const height = row.getBoundingClientRect().height;
        return height >= 50 && height <= (innerWidth <= 900 ? 95 : 65);
      }));
      if (innerWidth === 1200 && innerHeight === 900) check(`${id}TenVisible`, metrics[id].fullyVisible >= 10);
    }
    const badges = all('.badge').map(node => node.textContent);
    check('states', ['Working 1/2', 'Run finished', 'Needs input', 'Error', 'Unconfirmed'].every(text => badges.includes(text)));
    check('grouping', all('.card').length === 24 && all('.row-title').every(node => !node.textContent.startsWith('Child ')));
    check('visibleTimes', all('.finished > summary .compact-time').every(node =>
      node.textContent.startsWith('Parent alert: Finished') && node.getBoundingClientRect().height > 0)
      && all('.compact-time').some(node => node.textContent.startsWith('No parent alert observed; first seen')));
    check('parentAlertProvenance', all('#running .compact-time').filter(node => !node.textContent.startsWith('No parent'))
      .every(node => node.textContent.startsWith('Parent alert: Finished'))
      && !all('.parent-alert').some(node => node.textContent.includes('CHILD ALERT MUST NOT REPLACE PARENT')));
    check('readableFonts', all('.row-title').every(node => parseFloat(getComputedStyle(node).fontSize) >= 14)
      && all('.compact-time').every(node => parseFloat(getComputedStyle(node).fontSize) >= 13));
    const title = all('.row-title').find(node => node.textContent.startsWith('LongUnbroken'));
    const card = title.closest('.card');
    const summary = card.querySelector('summary');
    const closedHeight = card.getBoundingClientRect().height;
    check('longTitleTruncated', title.scrollWidth > title.clientWidth && getComputedStyle(title).textOverflow === 'ellipsis');
    summary.focus({ preventScroll: true });
    check('focusBeforePoll', document.activeElement === summary);
    summary.click();
    check('expands', card.open && card.getBoundingClientRect().height > closedHeight
      && card.querySelector('.full-title').textContent === title.textContent
      && card.querySelector('.full-title').getBoundingClientRect().height > 0
      && card.querySelector('.relatives').textContent.includes('Child: Child') && within());
    check('dormantNames', card.querySelector('.relatives').textContent.includes('Dormant full child name')
      && card.querySelector('.relatives').textContent.includes('Not observed')
      && card.querySelector('.depth-3').textContent.includes('Name unavailable'));
    await wait(1900);
    check('liveDisclosureAndFocus', card.isConnected && card.open && document.activeElement === summary
      && document.querySelector('#running').contains(card));
    summary.click();
    await wait(1900);
    check('collapses', !card.open && card.getBoundingClientRect().height === closedHeight);
    const dismissedCard = document.querySelector('.card.finished');
    const button = dismissedCard.querySelector('.dismiss');
    check('dismissAccessible', button.tagName === 'BUTTON' && button.type === 'button'
      && button.getAttribute('aria-label').includes('from this monitor only')
      && button.getBoundingClientRect().height >= 32 && button.getBoundingClientRect().width >= 44);
    button.focus({ preventScroll: true });
    await wait(1900);
    check('dismissFocusStable', document.activeElement === button && !dismissedCard.open);
    button.click();
    for (let attempt = 0; dismissedCard.isConnected && attempt < 60; attempt++) await wait(100);
    check('individualDismiss', !dismissedCard.isConnected && !dismissedCard.open && all('.card').length === 23
      && document.querySelector('#dismiss-result').textContent.includes('Copilot sessions and files are unchanged'));
    const clear = document.querySelector('#clear-finished');
    clear.click();
    for (let attempt = 0; all('.card.finished').length && attempt < 60; attempt++) await wait(100);
    check('bulkFinishedOnly', !all('.card.finished').length && all('#running > .card').length === 12
      && all('#retained > .card').length === 6 && clear.disabled);
    await wait(1900);
    check('noPollReappearance', all('.card').length === 18 && !all('.card.finished').length && within());
    return { mode, ...metrics, checks, passed: Object.values(checks).every(Boolean) };
  };
  run().catch(error => ({ mode, viewport: [innerWidth, innerHeight], passed: false, error: error.message }))
    .then(result => fetch('/results', { method: 'POST', body: JSON.stringify(result) }));
}

function host() {
  document.querySelector('#viewport').textContent = `Actual browser viewport: ${innerWidth} x ${innerHeight} CSS pixels`;
  const sizes = [[1200, 900], [900, 900], [901, 900], [360, 900], [320, 700], [innerWidth, innerHeight]];
  const cases = [];
  for (const [width, height] of [...new Map(sizes.map(size => [size.join('x'), size])).values()]) {
    for (const mode of ['light', 'dark']) cases.push({ width, height, mode });
  }
  const total = cases.length;
  let active;
  const next = () => {
    document.querySelector('iframe')?.remove();
    active = cases.shift();
    if (!active) return;
    const frame = document.createElement('iframe');
    frame.width = active.width;
    frame.height = active.height;
    frame.src = `/case?mode=${active.mode}&fixture=${crypto.randomUUID()}`;
    frame.title = `${active.width}x${active.height} ${active.mode} synthetic monitor fixture`;
    document.body.append(frame);
  };
  const render = async () => {
    const data = await (await fetch('/results')).json();
    document.querySelector('#results').textContent = data.map(row =>
      `${row.viewport.join('x')} ${row.mode}: ${row.passed ? 'PASS' : 'FAIL'} ${JSON.stringify(row.running)} ${JSON.stringify(row.retained)}`
    ).join('\n');
    document.title = `Density checks: ${data.filter(row => row.passed).length}/${total} passed`;
    if (active && data.some(row => row.viewport[0] === active.width && row.viewport[1] === active.height && row.mode === active.mode)) next();
  };
  next();
  setInterval(render, 1000);
}

const server = http.createServer(async (request, response) => {
  try {
    const url = new URL(request.url, 'http://127.0.0.1');
    response.setHeader('Cache-Control', 'no-store');
    if (url.pathname === '/results') {
      if (request.method === 'POST') {
        let body = '';
        for await (const chunk of request) {
          body += chunk;
          if (body.length > 20_000) throw new Error('Fixture result too large');
        }
        const value = JSON.parse(body);
        results.set(`${value.viewport.join('x')}/${value.mode}`, value);
      }
      response.setHeader('Content-Type', 'application/json');
      response.end(JSON.stringify([...results.values()]));
    } else if (url.pathname === '/api/status') {
      const { monitor, mode } = fixtureFor(request);
      response.setHeader('Content-Type', 'application/json');
      response.end(JSON.stringify({
        ...monitor.snapshot(), healthy: true, issues: [], coverage: 'SYNTHETIC FIXTURE - this machine only',
        machine: 'TEST-MACHINE', updatedAt: timestamp, notification: { message: 'Fixture: notifications not connected' },
        theme: { mode, source: 'windows-apps' },
      }));
    } else if (url.pathname === '/api/control' && request.method === 'GET') {
      response.setHeader('Content-Type', 'application/json');
      response.end(JSON.stringify({ token: 'synthetic-only' }));
    } else if (url.pathname === '/api/dismiss' && request.method === 'POST') {
      if (request.headers.authorization !== 'Bearer synthetic-only') {
        response.writeHead(403).end();
        return;
      }
      response.setHeader('Content-Type', 'application/json');
      response.end(JSON.stringify(await fixtureFor(request).actions.dismiss(await readDismissEntries(request))));
    } else if (url.pathname === '/') {
      response.setHeader('Content-Type', 'text/html');
      response.end(`<!doctype html><title>Density checks</title><h1>Synthetic compact-row checks</h1>
        <p id="viewport"></p><pre id="results">Measuring...</pre>
        <style>iframe{display:block;border:0;margin:12px 0}pre{white-space:pre-wrap}</style>
        <script src="/host.js"></script>`);
    } else if (url.pathname === '/host.js' || url.pathname === '/measure.js') {
      response.setHeader('Content-Type', 'text/javascript');
      response.end(url.pathname === '/host.js' ? `(${host})();` : `window.expected=${JSON.stringify(expected)};(${measure})();`);
    } else if (url.pathname === '/case' || ['/style.css', '/app.js'].includes(url.pathname)) {
      const file = url.pathname === '/case' ? 'index.html' : url.pathname.slice(1);
      let content = await readFile(new URL(`../public/${file}`, import.meta.url), 'utf8');
      if (file === 'index.html') content = content.replace('</body>', '<script src="/measure.js" defer></script></body>');
      response.setHeader('Content-Type', file.endsWith('.html') ? 'text/html' : file.endsWith('.css') ? 'text/css' : 'text/javascript');
      response.end(content);
    } else {
      response.writeHead(404).end();
    }
  } catch (error) {
    console.error(error);
    response.writeHead(500).end('Fixture failure; see console');
  }
});
server.listen(0, '127.0.0.1', () => console.log(`Synthetic UI checks: http://127.0.0.1:${server.address().port}`));
