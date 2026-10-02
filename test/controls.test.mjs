import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, cp, writeFile, readFile, rm } from 'node:fs/promises';
import { spawn } from 'node:child_process';
import { DatabaseSync } from 'node:sqlite';
import net from 'node:net';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';

test('real server controls protect fixture data and persist dismissal across a clean restart', {
  skip: process.platform !== 'win32', timeout: 60000,
}, async t => {
  const dir = await mkdtemp(path.join(os.tmpdir(), 'monitor-http-fixture-'));
  const app = path.join(dir, 'app'), home = path.join(dir, 'home');
  const source = path.join(home, '.copilot');
  await mkdir(path.join(source, 'session-state'), { recursive: true });
  await mkdir(path.join(app, '.local'), { recursive: true });
  for (const folder of ['src', 'public', 'windows']) {
    await cp(new URL(`../${folder}`, import.meta.url), path.join(app, folder), { recursive: true });
  }
  const db = new DatabaseSync(path.join(source, 'data.db'));
  db.exec(`
    CREATE TABLE sessions(id,title,is_running,was_interrupted,execution_location,session_type,archived_at);
    CREATE TABLE workspaces(id,session_id,creator_session_id,host_id,archived_at);
    CREATE TABLE workspace_parent_links(child_workspace_id,parent_workspace_id);
    CREATE TABLE workspace_session_aliases(session_id,workspace_id);
    CREATE TABLE workspace_side_chats(workspace_id,session_id);
    CREATE TABLE session_side_chats(parent_session_id,session_id);
  `);
  db.close();
  const at = '2026-10-02T20:00:00.000Z';
  const rows = ['finished', 'error'].map(state => ({
    id: state, title: `SYNTHETIC ${state}`, state, detail: 'Synthetic fixture', activity: 'Fixture',
    machine: 'TEST', source: 'Copilot desktop', runId: state, firstObservedAt: at, startedAt: at,
    lastEventAt: at, lastResponseAt: at, finishedAt: state === 'finished' ? at : null,
  }));
  await writeFile(path.join(app, '.local', 'sessions.json'), JSON.stringify({ version: 2, sessions: rows }));
  const keys = ['b'.repeat(64)];
  await writeFile(path.join(app, '.local', 'notifications.json'), JSON.stringify({ version: 1, keys }));
  const reservation = net.createServer();
  await new Promise(resolve => reservation.listen(0, '127.0.0.1', resolve));
  const port = reservation.address().port;
  await new Promise(resolve => reservation.close(resolve));
  assert.notEqual(port, 43187);
  const url = `http://127.0.0.1:${port}`;
  let child, exited, token, output = '';
  const request = (route, options) => fetch(`${url}${route}`, { ...options, signal: AbortSignal.timeout(5000) });
  const post = (route, body, headers = {}) => request(route, {
    method: 'POST', body: JSON.stringify(body), headers: { Authorization: `Bearer ${token}`, ...headers },
  });
  const stop = async () => {
    if (!child || child.exitCode !== null) return;
    token = (await (await request('/api/control')).json()).token;
    assert.equal((await post('/api/stop')).status, 200);
    await exited;
  };
  t.after(async () => {
    try { await stop(); } finally {
      if (child && child.exitCode === null) child.kill();
      await rm(dir, { recursive: true, force: true });
    }
  });
  const start = async () => {
    child = spawn(process.execPath, ['--disable-warning=ExperimentalWarning', '--input-type=module', '-e',
      "import os from 'node:os'; import assert from 'node:assert/strict'; assert.equal(os.homedir(),process.env.USERPROFILE); await import('./src/server.mjs');"],
    { cwd: app, env: { ...process.env, USERPROFILE: home, MONITOR_PORT: String(port) }, windowsHide: true });
    exited = new Promise(resolve => child.once('exit', resolve));
    child.stdout.on('data', data => { output = (output + data).slice(-4000); });
    child.stderr.on('data', data => { output = (output + data).slice(-4000); });
    for (let attempt = 0; attempt < 60; attempt++) {
      await new Promise(resolve => setTimeout(resolve, 250));
      assert.equal(child.exitCode, null, output);
      if (attempt === 0) continue;
      let response;
      try { response = await request('/api/status'); } catch (error) {
        if (error.cause?.code !== 'ECONNREFUSED') throw error;
        continue;
      }
      const status = await response.json();
      if (status.healthy) {
        assert.equal(status.desktopSessions, 0, 'Must use the empty synthetic database');
        token = (await (await request('/api/control')).json()).token;
        return status;
      }
    }
    assert.fail(`Fixture observer did not become healthy: ${output}`);
  };
  const initial = await start();
  const target = initial.sessions.find(item => item.state === 'finished');
  const payload = { entries: [{ id: target.id, key: target.dismissKey }] };
  assert.equal((await request('/api/dismiss')).status, 404);
  assert.equal((await request('/api/dismiss', { method: 'POST', body: JSON.stringify(payload) })).status, 403);
  assert.equal((await post('/api/dismiss', payload, { Origin: 'https://invalid.example' })).status, 403);
  const invalidHost = await new Promise((resolve, reject) => {
    const req = http.request(`${url}/api/dismiss`, {
      method: 'POST', headers: { Host: 'invalid.example', Authorization: `Bearer ${token}` },
    }, res => {
      res.resume();
      res.once('end', () => resolve(res.statusCode));
    });
    req.on('error', reject);
    req.setTimeout(5000, () => req.destroy(new Error('Fixture request timed out')));
    req.end(JSON.stringify(payload));
  });
  assert.equal(invalidHost, 403);
  assert.equal((await post('/api/dismiss', { entries: [] })).status, 400);
  assert.equal((await (await request('/api/status')).json()).sessions.length, 2);
  assert.deepEqual((await (await post('/api/dismiss', payload)).json()).dismissed, ['finished']);
  assert.equal((await (await request('/api/status')).json()).sessions[0].state, 'error');
  await stop();
  const restarted = await start();
  assert.equal(restarted.sessions.length, 1);
  assert.equal(restarted.members.length, 2);
  assert.equal(restarted.notification.state, 'ready');
  assert.deepEqual(JSON.parse(await readFile(path.join(app, '.local', 'notifications.json'), 'utf8')).keys, keys);
});
