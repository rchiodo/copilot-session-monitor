import https from 'node:https';
import { createHash, timingSafeEqual, X509Certificate } from 'node:crypto';
import { isIP } from 'node:net';
import { checkServerIdentity } from 'node:tls';

export const VERSION = 1;
export const HEARTBEAT_MS = 15000;
export const MAX_BYTES = 4 * 1024 * 1024;
export const UUID = /^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/i;
export const HASH = /^[a-f0-9]{64}$/;
export const digest = value => createHash('sha256').update(value).digest('hex');
export const fail = (message, status = 400) => Object.assign(new Error(message), { status });

function object(value, keys) {
  if (!value || typeof value !== 'object' || Array.isArray(value) ||
      Object.keys(value).some(key => !keys.includes(key))) throw fail('Unexpected metadata fields');
}
function text(value, max = 512, nullable = false) {
  if (nullable && value === null) return;
  if (typeof value !== 'string' || !value.length || value.length > max || /[\x00-\x1f]/.test(value)) {
    throw fail('Invalid metadata string');
  }
}
function id(value, nullable = false) {
  if (nullable && value === null) return;
  text(value, 128);
  if (!/^[a-zA-Z0-9_.:-]+$/.test(value)) throw fail('Invalid session identity');
}
function date(value, nullable = false) {
  if (nullable && value === null) return;
  if (typeof value !== 'string' || !/^\d{4}-\d\d-\d\dT/.test(value) ||
      value.length > 35 || !Number.isFinite(Date.parse(value))) throw fail('Invalid metadata timestamp');
}
const memberFields = ['id', 'title', 'source', 'state', 'detail', 'activity', 'runId', 'firstObservedAt',
  'startedAt', 'lastEventAt', 'lastResponseAt', 'finishedAt', 'parentId', 'hierarchyIssue', 'contextOnly', 'lastAlert', 'completionTracked'];
const relativeFields = ['id', 'title', 'parentId', 'aliasOf', 'hierarchyIssue', 'detail'];
const states = ['working', 'finished', 'waiting', 'error', 'unknown', 'idle'];
const kinds = ['finished', 'waiting', 'error', 'warning'];
const pick = (value, fields) => Object.fromEntries(fields.map(key => [key, value[key] ?? null]));

export function metadata(snapshot) {
  return {
    members: snapshot.members.map(row => ({ ...pick(row, memberFields),
      completionTracked: row.completionTracked ?? row.state === 'working' })),
    relatives: (snapshot.relatives ?? []).map(row => pick(row, relativeFields)),
  };
}

export function observedMetadata(snapshot, samples, tracked = new Map()) {
  const current = new Map(samples.map(sample => [sample.id, sample]));
  const members = snapshot.members.map(row => {
    const sample = current.get(row.id);
    // A local historical finish is not current remote completion authority.
    if (row.state === 'finished' && (!sample?.alive || sample.readError || sample.completionUnconfirmed ||
        !sample.events?.terminal || sample.events.closed || sample.events.replaced)) {
      return { ...row, completionTracked: false, state: 'unknown', finishedAt: null,
        detail: 'Finished run owner/evidence is unavailable; current status unconfirmed' };
    }
    return { ...row, completionTracked: tracked.has(row.id) };
  });
  return metadata({ ...snapshot, members });
}

export function validateConnect(value) {
  object(value, ['version', 'reporterId', 'installationId', 'bootId', 'generation']);
  if (value.version !== VERSION || ![value.reporterId, value.installationId, value.bootId].every(x => UUID.test(x)) ||
      !Number.isSafeInteger(value.generation) || value.generation < 1) throw fail('Invalid watcher handshake');
  return value;
}

export function validateReport(value) {
  object(value, ['version', 'reporterId', 'lease', 'seq', 'sentAt', 'healthy', 'issues', 'members', 'relatives', 'notices']);
  if (value.version !== VERSION || !UUID.test(value.reporterId) || !HASH.test(value.lease) ||
      !Number.isSafeInteger(value.seq) || value.seq < 1 || typeof value.healthy !== 'boolean') throw fail('Invalid report envelope');
  date(value.sentAt);
  for (const key of ['members', 'relatives', 'notices', 'issues']) {
    if (!Array.isArray(value[key]) || value[key].length > (key === 'issues' ? 100 : 5000)) throw fail('Metadata limit exceeded');
  }
  value.issues.forEach(issue => text(issue, 1024));
  for (const row of value.members) {
    object(row, memberFields);
    id(row.id); id(row.parentId, true); id(row.runId, true);
    text(row.title); text(row.detail, 1024); text(row.activity);
    text(row.hierarchyIssue, 1024, true);
    if (!['Copilot desktop', 'CLI (activity only)'].includes(row.source) ||
        !states.includes(row.state) || typeof row.contextOnly !== 'boolean' ||
        typeof row.completionTracked !== 'boolean' ||
        row.completionTracked && !['working', 'waiting', 'unknown'].includes(row.state)) throw fail('Invalid member status');
    for (const key of ['firstObservedAt', 'startedAt', 'lastEventAt', 'lastResponseAt', 'finishedAt']) {
      date(row[key], key !== 'firstObservedAt');
    }
    if ((row.state === 'finished') !== (row.finishedAt !== null)) throw fail('Invalid completion timestamp');
    if (row.lastAlert !== null) {
      const alert = row.lastAlert;
      object(alert, ['sessionId', 'key', 'kind', 'message', 'at']);
      if (alert.sessionId !== row.id || !kinds.includes(alert.kind)) throw fail('Invalid parent alert provenance');
      text(alert.key, 2048); text(alert.message, 1024); date(alert.at);
    }
  }
  for (const row of value.relatives) {
    object(row, relativeFields);
    id(row.id); id(row.parentId, true); id(row.aliasOf, true); text(row.title);
    text(row.detail, 1024, true); text(row.hierarchyIssue, 1024, true);
  }
  for (const rows of [value.members, value.relatives]) {
    if (new Set(rows.map(row => row.id)).size !== rows.length) throw fail('Duplicate member identity');
  }
  for (const notice of value.notices) {
    object(notice, ['key', 'familyId', 'kind']);
    id(notice.familyId);
    if (!HASH.test(notice.key) || !kinds.includes(notice.kind)) throw fail('Invalid monitor notice');
  }
  return value;
}

export async function readJson(request) {
  if (request.headers['content-type'] !== 'application/json' || request.headers['content-encoding']) {
    throw fail('Uncompressed application/json required', 415);
  }
  if (Number(request.headers['content-length']) > MAX_BYTES) throw fail('Report too large', 413);
  let size = 0;
  const chunks = [];
  for await (const chunk of request) {
    size += chunk.length;
    if (size > MAX_BYTES) throw fail('Report too large', 413);
    chunks.push(chunk);
  }
  try { return JSON.parse(Buffer.concat(chunks).toString('utf8')); }
  catch { throw fail('Invalid report JSON'); }
}

export function authorized(secret, expectedHash) {
  if (typeof secret !== 'string' || !/^Bearer [a-f0-9]{64}$/.test(secret) || !HASH.test(expectedHash)) return false;
  return timingSafeEqual(Buffer.from(digest(secret.slice(7)), 'hex'), Buffer.from(expectedHash, 'hex'));
}

export function privateAddress(value) {
  if (value === '::1') return true;
  if (isIP(value) === 6) return /^(fc|fd|fe[89ab])/i.test(value);
  if (isIP(value) !== 4) return false;
  const [a, b] = value.split('.').map(Number);
  return a === 127 || a === 10 || a === 172 && b >= 16 && b <= 31 || a === 192 && b === 168 ||
    a === 169 && b === 254 || a === 100 && b >= 64 && b <= 127;
}

export function validatePairing(value) {
  object(value, ['version', 'reporterId', 'label', 'collectorUrl', 'token', 'certificate']);
  if (value.version !== VERSION || !UUID.test(value.reporterId) || !HASH.test(value.token)) throw fail('Invalid pairing');
  text(value.label, 80);
  const url = new URL(value.collectorUrl);
  if (url.protocol !== 'https:' || url.username || url.password || url.search || url.hash || url.pathname !== '/' ||
      !privateAddress(url.hostname.replace(/^\[|\]$/g, '')) || !url.port || Number(url.port) < 1024) {
    throw fail('Pairing requires an HTTPS private/loopback IP and explicit port');
  }
  if (typeof value.certificate !== 'string' || value.certificate.length > 16384 ||
      (value.certificate.match(/-----BEGIN CERTIFICATE-----/g) ?? []).length !== 1) throw fail('Exactly one collector certificate is required');
  const cert = new X509Certificate(value.certificate);
  if (Date.parse(cert.validTo) <= Date.now()) throw fail('Pairing certificate expired');
  return value;
}

export const CONNECTION_PREFIX = 'csm1:';

export function encodeConnectionString(pairing) {
  validatePairing(pairing);
  return CONNECTION_PREFIX + Buffer.from(JSON.stringify(pairing), 'utf8').toString('base64url');
}

export function decodeConnectionString(value) {
  if (typeof value !== 'string') throw fail('Connection string is required');
  const trimmed = value.trim();
  if (!trimmed.startsWith(CONNECTION_PREFIX)) throw fail('Unrecognized connection string format');
  if (trimmed.length > 32768) throw fail('Connection string is too large');
  let parsed;
  try { parsed = JSON.parse(Buffer.from(trimmed.slice(CONNECTION_PREFIX.length), 'base64url').toString('utf8')); }
  catch { throw fail('Malformed connection string'); }
  return validatePairing(parsed);
}

export function request(pairing, route, body) {
  const data = JSON.stringify(body);
  if (Buffer.byteLength(data) > MAX_BYTES) return Promise.reject(fail('Report too large', 413));
  return new Promise((resolve, reject) => {
    const req = https.request(new URL(route, pairing.collectorUrl), {
      method: 'POST', ca: pairing.certificate, minVersion: 'TLSv1.2',
      checkServerIdentity: (hostname, certificate) => {
        const invalidName = checkServerIdentity(hostname, certificate);
        if (invalidName) return invalidName;
        if (!certificate.raw || digest(certificate.raw) !== digest(new X509Certificate(pairing.certificate).raw)) {
          return fail('Collector certificate pin mismatch');
        }
      },
      headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(data),
        'X-Monitor-Reporter': pairing.reporterId, Authorization: `Bearer ${pairing.token}` },
    }, res => {
      let content = '', size = 0;
      res.on('data', chunk => {
        size += chunk.length;
        if (size > 65536) res.destroy(fail('Collector response too large'));
        else content += chunk.toString();
      });
      res.on('error', reject);
      res.on('end', () => {
        if (res.statusCode !== 200) return reject(fail(`Collector rejected request (${res.statusCode})`, res.statusCode));
        try { resolve(JSON.parse(content)); } catch { reject(fail('Invalid collector response')); }
      });
    });
    req.setTimeout(4000, () => req.destroy(fail('Collector request timed out')));
    req.on('error', error => reject(fail(`Collector transport unavailable (${error.code ?? error.name})`, error.status ?? 503)));
    req.end(data);
  });
}
