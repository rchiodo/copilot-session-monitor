import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import vm from 'node:vm';

const code = await readFile(new URL('../public/app.js', import.meta.url), 'utf8');
const timestamp = '2026-10-02T21:00:00.000Z';
const session = (state, id = state) => ({
  id, state, title: `${state} session`, source: 'Copilot desktop', machine: 'TEST',
  startedAt: timestamp, firstObservedAt: timestamp, lastResponseAt: timestamp,
  finishedAt: state === 'finished' ? timestamp : null, activity: 'Executing tools',
  detail: state === 'waiting' ? 'Input needed' : state === 'finished' ? 'Current run finished; not task success' : state,
});
const part = (node, className) => node.className?.split(' ').includes(className) ? node
  : node.children.map(child => part(child, className)).find(Boolean);
const badgeText = card => part(card, 'badge').textContent;
const titleText = card => part(card, 'row-title').textContent;
const contents = node => node.textContent + node.children.map(contents).join('\n');

async function page(sessions) {
  const nodes = new Map();
  const node = (tag = 'div') => ({
    tagName: tag.toUpperCase(), textContent: '', children: [], hidden: false, handlers: {}, attributes: {},
    parent: null, open: false,
    get isConnected() { return this.root || this.parent?.isConnected || false; },
    append(...children) { for (const child of children) this.insertBefore(child, null); },
    insertBefore(child, before) {
      child.remove();
      const index = before ? this.children.indexOf(before) : this.children.length;
      assert.ok(index >= 0);
      this.children.splice(index, 0, child);
      child.parent = this;
    },
    remove() {
      if (!this.parent) return;
      if (this === document.activeElement || this.children.includes(document.activeElement)) document.activeElement = null;
      this.parent.children.splice(this.parent.children.indexOf(this), 1);
      this.parent = null;
    },
    focus() { document.activeElement = this; },
    setAttribute(key, value) { this.attributes[key] = value; },
    addEventListener(event, handler) { this.handlers[event] = handler; },
  });
  const document = {
    title: '', createElement: node, documentElement: { dataset: {} }, activeElement: null,
    getElementById(id) {
      if (!nodes.has(id)) {
        const element = node();
        element.root = true;
        nodes.set(id, element);
      }
      return nodes.get(id);
    },
  };
  const pending = [];
  const fixture = {
    fail: false, document, nodes,
    requests: [],
    status: {
      sessions, healthy: true, issues: [], coverage: 'This machine only',
      machine: 'TEST', updatedAt: timestamp, notification: { message: 'Ready' },
      theme: { mode: 'light', source: 'windows-apps' },
    },
    poll: () => pending.shift()(),
  };
  vm.runInNewContext(code, {
    document, Date, AbortSignal,
    setTimeout: fn => pending.push(fn),
    fetch: async (_url, options) => {
      assert.ok(options.signal);
      if (fixture.fail) throw new Error('Disconnected');
      if (_url === '/api/control') return { ok: true, json: async () => ({ token: 'fixture-only' }) };
      if (_url === '/api/dismiss') {
        assert.equal(options.method, 'POST');
        const body = JSON.parse(options.body);
        fixture.requests.push(body);
        fixture.status.sessions = fixture.status.sessions.filter(row => !body.entries.some(entry => row.id === entry.id));
        return { ok: true, json: async () => ({ dismissed: body.entries.map(entry => entry.id), skipped: [] }) };
      }
      return { ok: true, json: async () => fixture.status };
    },
  });
  await new Promise(resolve => setImmediate(resolve));
  return fixture;
}

test('UI retains rows on disconnect but marks live/waiting states unconfirmed, then recovers', async () => {
  const fixture = await page([session('finished'), session('working'), session('waiting')]);
  const { nodes, document } = fixture;
  assert.equal(nodes.get('running').children.length, 1);
  assert.equal(nodes.get('retained').children.length, 2);
  assert.match(document.title, /1 working, 3 retained/);
  fixture.fail = true;
  await fixture.poll();
  assert.equal(nodes.get('running').children.length, 0);
  assert.equal(nodes.get('running-empty').hidden, false);
  assert.match(nodes.get('running-empty').textContent, /status unavailable/);
  assert.equal(nodes.get('retained').children.length, 3);
  assert.deepEqual(nodes.get('retained').children.map(badgeText),
    ['Run finished', 'Unconfirmed', 'Unconfirmed']);
  assert.match(document.title, /unavailable/);
  assert.match(nodes.get('summary').textContent, /^0 working/);
  fixture.fail = false;
  await fixture.poll();
  assert.match(document.title, /1 working, 3 retained/);
  assert.deepEqual(nodes.get('running').children.map(badgeText), ['Working']);
  assert.deepEqual(nodes.get('retained').children.map(badgeText),
    ['Run finished', 'Waiting for input']);
  fixture.status.healthy = false;
  await fixture.poll();
  assert.equal(nodes.get('running').children.length, 0);
  assert.deepEqual(nodes.get('retained').children.map(badgeText),
    ['Run finished', 'Unconfirmed', 'Unconfirmed']);
});

test('UI preserves server response ordering, explicit completion time, fallback and safe title rendering', async () => {
  const finished = { ...session('finished'), title: '<script>not executable</script>' };
  const noResponse = { ...session('working'), lastResponseAt: null };
  const { nodes } = await page([finished, noResponse, session('error'), session('unknown')]);
  const cards = nodes.get('retained').children;
  const working = nodes.get('running').children[0];
  assert.equal(titleText(cards[0]), finished.title);
  assert.equal(part(cards[0], 'row-title').innerHTML, undefined);
  assert.equal(part(cards[0], 'row-title').title, finished.title);
  assert.equal(part(cards[0], 'full-title').textContent, finished.title);
  assert.match(part(cards[0], 'timestamp').textContent, /^Last assistant response:/);
  assert.equal(part(cards[0], 'finished-time').textContent, `Finished (observed): ${new Date(timestamp).toLocaleString(undefined, {
    dateStyle: 'short', timeStyle: 'short',
  })}`);
  assert.equal(part(cards[0], 'row-details').children[4].textContent, `Run finished (observed): ${new Date(timestamp).toLocaleString(undefined, {
    dateStyle: 'medium', timeStyle: 'medium',
  })}`);
  assert.match(part(working, 'timestamp').textContent, /^No assistant response recorded.*ordering fallback/);
  assert.match(part(working, 'compact-time').textContent, /^First seen:.*\(no response\)$/);
  assert.equal(part(working, 'finished-time').hidden, true);
  assert.equal(badgeText(cards[1]), 'Error');
  assert.equal(badgeText(cards[2]), 'Unconfirmed');
});

test('columns preserve response order, counts, state transitions and independent empty states', async () => {
  const fixture = await page([]);
  const { nodes } = fixture;
  for (const id of ['running', 'retained']) {
    assert.equal(nodes.get(`${id}-count`).textContent, 0);
    assert.equal(nodes.get(`${id}-empty`).hidden, false);
  }
  const ordered = [
    session('waiting', 'new-wait'), session('working', 'new-work'),
    session('finished', 'old-finish'), session('working', 'old-work'),
    { ...session('error', 'interrupted'), detail: 'Run interrupted' }, session('unknown'),
  ].map((item, index) => ({ ...item, title: item.id, lastResponseAt: new Date(Date.parse(timestamp) - index * 1000).toISOString() }));
  fixture.status.sessions = ordered;
  await fixture.poll();
  const titles = id => nodes.get(id).children.map(titleText);
  assert.deepEqual(titles('running'), ['new-work', 'old-work']);
  assert.deepEqual(titles('retained'), ['new-wait', 'old-finish', 'interrupted', 'unknown']);
  assert.equal(badgeText(nodes.get('retained').children[2]), 'Interrupted');
  assert.equal(nodes.get('running-count').textContent, 2);
  assert.equal(nodes.get('retained-count').textContent, 4);
  assert.equal(nodes.get('running-empty').hidden, true);
  assert.equal(nodes.get('retained-empty').hidden, true);
  fixture.status.sessions = ordered.map(item => ({ ...item, state: 'finished', finishedAt: timestamp }));
  await fixture.poll();
  assert.deepEqual(titles('running'), []);
  assert.equal(nodes.get('running-empty').hidden, false);
  assert.equal(nodes.get('retained-count').textContent, 6);
  fixture.status.sessions = ordered.map(item => ({ ...item, state: 'working', finishedAt: null }));
  await fixture.poll();
  assert.equal(nodes.get('running-count').textContent, 6);
  assert.equal(nodes.get('retained-empty').hidden, false);
});

test('native disclosure, focus and full values survive polling, sorting and movement between columns', async () => {
  const fixture = await page([session('working', 'first'), session('working', 'second')]);
  const card = fixture.nodes.get('running').children[0];
  const summary = part(card, 'row-summary');
  assert.equal(card.tagName, 'DETAILS');
  assert.equal(summary.tagName, 'SUMMARY');
  assert.equal(card.open, false);
  assert.equal(part(card, 'disclosure-icon').attributes['aria-hidden'], 'true');
  card.open = true;
  summary.focus();
  await fixture.poll();
  assert.equal(fixture.nodes.get('running').children[0], card);
  assert.equal(card.open, true);
  assert.equal(fixture.document.activeElement, summary);
  fixture.status.sessions.reverse();
  await fixture.poll();
  assert.equal(fixture.nodes.get('running').children[1], card);
  assert.equal(fixture.document.activeElement, summary);
  fixture.status.sessions[1] = {
    ...session('waiting', 'first'), detail: 'Plan approval needed',
    title: 'A full updated long title', machine: 'Full machine value',
  };
  await fixture.poll();
  assert.equal(fixture.nodes.get('retained').children[0], card);
  assert.equal(card.open, true);
  assert.equal(fixture.document.activeElement, summary);
  assert.equal(badgeText(card), 'Plan approval needed');
  assert.match(part(card, 'compact-time').textContent, /^Response:/);
  assert.equal(part(card, 'full-title').textContent, 'A full updated long title');
  assert.match(part(card, 'muted').textContent, /Full machine value/);
  card.open = false;
  await fixture.poll();
  assert.equal(card.open, false);
});

test('one family card separates aggregate work from the parent alert and exposes member statuses', async () => {
  const parent = { ...session('finished', 'p'), title: 'Parent title', detail: 'Current run finished' };
  const child = { ...session('working', 'c'), title: 'Child title' };
  const ownAlert = { sessionId: 'p', kind: 'finished', at: timestamp, message: 'Parent current run finished' };
  const family = { ...parent, state: 'working', finishedAt: null, parentAlert: ownAlert,
    members: [parent, child], childCount: 1, runningCount: 1, detail: 'One descendant working' };
  const fixture = await page([family]);
  const card = fixture.nodes.get('running').children[0];
  assert.equal(fixture.nodes.get('running').children.length, 1);
  assert.equal(fixture.nodes.get('retained').children.length, 0);
  assert.equal(titleText(card), 'Parent title');
  assert.equal(badgeText(card), 'Working 1/2');
  assert.match(part(card, 'compact-time').textContent, /^Parent alert: Finished - /);
  assert.match(part(card, 'parent-alert').textContent, /Parent current run finished/);
  assert.match(contents(part(card, 'relatives')), /Child: Child title - Working/);
  fixture.fail = true;
  await fixture.poll();
  assert.equal(badgeText(card), 'Unconfirmed');
  assert.match(part(card, 'compact-time').textContent, /^Parent alert: Finished/);
  assert.match(contents(part(card, 'relatives')), /Child title - Unconfirmed/);
  fixture.fail = false;
  fixture.status.sessions = [{ ...family, parentAlert: null, lastResponseAt: null, hierarchyIssue: 'Recorded parent is missing' }];
  await fixture.poll();
  assert.match(part(card, 'compact-time').textContent, /^No parent alert observed; first seen/);
  assert.equal(part(card, 'hierarchy-warning').hidden, false);
  assert.match(part(card, 'hierarchy-warning').textContent, /Recorded parent is missing/);
});

test('dismiss controls are distinct native buttons, only finished, and never toggle disclosure', async () => {
  const finished = { ...session('finished'), dismissKey: 'a'.repeat(64) };
  const fixture = await page([finished, session('waiting'), session('error'), session('unknown'), session('working')]);
  const card = fixture.nodes.get('retained').children[0], button = part(card, 'dismiss');
  assert.equal(button.tagName, 'BUTTON');
  assert.equal(button.type, 'button');
  assert.match(button.attributes['aria-label'], /from this monitor only/);
  assert.equal(button.hidden, false);
  assert.equal(fixture.nodes.get('clear-finished').disabled, false);
  assert.equal(fixture.nodes.get('retained').children.slice(1).every(row => part(row, 'dismiss').hidden), true);
  card.open = true;
  button.focus();
  await fixture.poll();
  assert.equal(fixture.document.activeElement, button);
  let prevented = false, stopped = false;
  await button.handlers.click({ preventDefault() { prevented = true; }, stopPropagation() { stopped = true; } });
  assert.ok(prevented && stopped);
  assert.equal(card.open, true);
  assert.equal(fixture.requests.length, 1);
  assert.equal(fixture.requests[0].entries[0].id, finished.id);
  assert.equal(fixture.nodes.get('retained').children.length, 3);
  assert.equal(fixture.nodes.get('clear-finished').disabled, true);
  assert.equal(fixture.document.activeElement, fixture.nodes.get('dismiss-result'));
  assert.match(fixture.nodes.get('dismiss-result').textContent, /Copilot sessions and files are unchanged/);
});

test('bulk dismissal sends only visible finished revisions; full dormant descendant names wrap in details', async () => {
  const family = { ...session('finished'), dismissKey: 'a'.repeat(64), members: [session('finished')], childCount: 0,
    relatives: [{ ...session('finished'), depth: 0 },
      { id: 'dormant', title: 'Full dormant child name', state: 'unobserved', detail: 'Execution not observed', depth: 1 },
      { id: 'nested', title: 'Nested full name', state: 'unobserved', detail: 'Name metadata unavailable', hierarchyIssue: 'Missing link', depth: 2 }] };
  const fixture = await page([family, session('unknown')]);
  const detail = part(fixture.nodes.get('retained').children[0], 'relatives');
  assert.match(contents(detail), /Full dormant child name - Not observed/);
  assert.match(contents(detail), /Descendant \(level 2\): Nested full name/);
  assert.equal(detail.children[2].className, 'relative depth-2');
  fixture.status.healthy = false;
  await fixture.poll();
  assert.equal(fixture.nodes.get('clear-finished').disabled, true);
  fixture.status.healthy = true;
  await fixture.poll();
  await fixture.nodes.get('clear-finished').handlers.click();
  assert.equal(fixture.requests[0].entries.length, 1);
  assert.equal(fixture.status.sessions[0].state, 'unknown');
});
test('responsive column markup and styles allow wrapping and stack on narrow viewports', async () => {
  const [html, css] = await Promise.all(['index.html', 'style.css'].map(file =>
    readFile(new URL(`../public/${file}`, import.meta.url), 'utf8')));
  assert.match(html, /aria-labelledby="running-heading"/);
  assert.match(html, /aria-labelledby="retained-heading"/);
  assert.match(html, /Errors\/unconfirmed stay. Dismiss is monitor-only/);
  assert.match(css, /\.session-columns\s*\{[^}]*grid-template-columns:\s*repeat\(2,\s*minmax\(0,\s*1fr\)\)/);
  assert.match(css, /@media \(max-width: 900px\)\s*\{\s*\.session-columns\s*\{\s*grid-template-columns:\s*minmax\(0,\s*1fr\)/);
  assert.match(css, /\.session-column\s*\{\s*min-width:\s*0/);
  assert.match(css, /\.card\s*\{[^}]*overflow-wrap:\s*anywhere/);
  assert.match(css, /\.row-title\s*\{[^}]*font-size:\s*14px[^}]*text-overflow:\s*ellipsis/);
  assert.match(css, /\.compact-time\s*\{[^}]*font-size:\s*13px/);
  assert.match(css, /summary:focus-visible/);
});
test('UI follows successive Windows app theme reports and explicitly falls back when unavailable', async () => {
  const fixture = await page([]);
  const { document, nodes } = fixture;
  assert.equal(document.documentElement.dataset.theme, 'light');
  assert.match(nodes.get('theme').textContent, /Windows app preference \(light\)/);
  fixture.status.theme = { mode: 'dark', source: 'windows-apps' };
  await fixture.poll();
  assert.equal(document.documentElement.dataset.theme, 'dark');
  fixture.status.theme = { mode: null, source: 'browser-fallback', reason: 'Registry unavailable' };
  await fixture.poll();
  assert.equal(document.documentElement.dataset.theme, undefined);
  assert.match(nodes.get('theme').textContent, /browser fallback.*Registry unavailable/);
  fixture.status.theme = { mode: 'light', source: 'windows-apps' };
  await fixture.poll();
  assert.equal(document.documentElement.dataset.theme, 'light');
  fixture.fail = true;
  await fixture.poll();
  assert.equal(document.documentElement.dataset.theme, undefined);
});

test('light and dark palettes meet 4.5:1 contrast for text, controls, timestamps and every status', async () => {
  const css = await readFile(new URL('../public/style.css', import.meta.url), 'utf8');
  assert.match(css, /:root\s*\{[\s\S]*color-scheme: light dark/);
  assert.match(css, /:root\[data-theme="light"\]\s*\{\s*color-scheme: light/);
  assert.match(css, /:root\[data-theme="dark"\]\s*\{\s*color-scheme: dark/);
  const palette = Object.fromEntries([...css.matchAll(/--([\w-]+): light-dark\((#[a-f0-9]{6}), (#[a-f0-9]{6})\)/g)]
    .map(([, key, light, dark]) => [key, [light, dark]]));
  const luminance = hex => {
    const channels = hex.slice(1).match(/../g).map(channel => parseInt(channel, 16) / 255)
      .map(value => value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4);
    return channels[0] * 0.2126 + channels[1] * 0.7152 + channels[2] * 0.0722;
  };
  const pairs = [
    ['text', 'page'], ['text', 'card'], ['text', 'control'], ['text', 'hover'],
    ['muted', 'page'], ['muted', 'card'], ['finished-text', 'card'],
    ...['working', 'finished', 'warning', 'error'].map(state => [`${state}-text`, `${state}-bg`]),
  ];
  for (const [foreground, background] of pairs) {
    for (const mode of [0, 1]) {
      const values = [luminance(palette[foreground][mode]), luminance(palette[background][mode])].sort((a, b) => b - a);
      const ratio = (values[0] + 0.05) / (values[1] + 0.05);
      assert.ok(ratio >= 4.5, `${mode ? 'dark' : 'light'} ${foreground} on ${background}: ${ratio}`);
    }
  }
});
