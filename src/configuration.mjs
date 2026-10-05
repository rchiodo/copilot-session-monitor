import path from 'node:path';
import os from 'node:os';
import { fileURLToPath } from 'node:url';
import { randomUUID, randomBytes, X509Certificate } from 'node:crypto';
import { readFile, writeFile, rename, mkdir, copyFile, stat } from 'node:fs/promises';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { digest, HASH, UUID, privateAddress, validatePairing, encodeConnectionString } from './protocol.mjs';

export const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
export const dataDir = path.resolve(process.env.MONITOR_DATA_DIR ?? path.join(root, '.local'));
export const powershell = path.join(process.env.SystemRoot ?? 'C:\\Windows', 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe');
const execute = promisify(execFile);
async function protectData() {
  if ([path.parse(dataDir).root, root, os.homedir(), os.tmpdir()].some(value => path.resolve(value).toLowerCase() === dataDir.toLowerCase())) {
    throw new Error('Refusing to use a broad directory for private monitor data');
  }
  await mkdir(dataDir, { recursive: true });
  await execute(powershell, ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File',
    path.join(root, 'windows', 'protect-data.ps1'), '-DataDirectory', dataDir], { windowsHide: true });
}
export async function readConfig(file) { return JSON.parse(await readFile(path.join(dataDir, file), 'utf8')); }
export async function optionalConfig(file) {
  try { return await readConfig(file); } catch (error) { if (error.code === 'ENOENT') return null; throw error; }
}
export async function saveConfig(file, value) {
  await mkdir(dataDir, { recursive: true });
  const target = path.join(dataDir, file);
  await writeFile(`${target}.tmp`, JSON.stringify(value), { encoding: 'utf8', mode: 0o600 });
  await rename(`${target}.tmp`, target);
}
export function validateCollector(config) {
  if (config.version !== 1 || !UUID.test(config.id) || !privateAddress(config.bindAddress) ||
      !Number.isInteger(config.port) || config.port < 1024 || config.port > 65535 ||
      !Array.isArray(config.reporters) || config.reporters.length > 100 ||
      config.reporters.some(row => !UUID.test(row.id) || typeof row.label !== 'string' || !row.label.length ||
        row.label.length > 80 || /[\x00-\x1f]/.test(row.label) || !HASH.test(row.tokenHash) ||
        typeof row.legacy !== 'boolean') ||
      new Set(config.reporters.map(row => row.id)).size !== config.reporters.length ||
      config.reporters.filter(row => row.legacy).length > 1) throw new Error('Invalid collector configuration');
  return config;
}
export const loadCollector = async () => validateCollector(await readConfig('collector.json'));

export async function initialize(bindAddress = '127.0.0.1', port = 43188, reconfigure = false) {
  const existing = await optionalConfig('collector.json');
  if (existing && !reconfigure) return validateCollector(existing);
  if (await optionalConfig('runtime.json')) throw new Error('Stop the collector before initializing or changing its listener');
  const addresses = Object.values(os.networkInterfaces()).flat().map(row => row.address);
  if (!privateAddress(bindAddress) || !addresses.includes(bindAddress)) throw new Error('Select an assigned local/private IP (never an all-interface address)');
  const config = validateCollector({ version: 1, id: existing?.id ?? randomUUID(), bindAddress, port,
    reporters: existing?.reporters ?? [] });
  await protectData();
  await execute(powershell, ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File',
    path.join(root, 'windows', 'certificate.ps1'), '-DataDirectory', dataDir, '-BindAddress', bindAddress],
  { windowsHide: true });
  await saveConfig('collector.json', config);
  const local = await optionalConfig('watcher.json');
  if (local && config.reporters.some(row => row.id === local.reporterId && row.legacy)) {
    local.certificate = await readFile(path.join(dataDir, 'collector-cert.pem'), 'utf8');
    local.collectorUrl = `https://127.0.0.1:${port}`;
    await saveConfig('watcher.json', local);
  }
  for (const reporter of config.reporters.filter(row => !row.legacy)) {
    const file = `pairing-${reporter.id}.json`;
    const bundle = await optionalConfig(file);
    if (bundle && digest(bundle.token) === reporter.tokenHash) {
      bundle.certificate = await readFile(path.join(dataDir, 'collector-cert.pem'), 'utf8');
      bundle.collectorUrl = `https://${bindAddress.includes(':') ? `[${bindAddress}]` : bindAddress}:${port}`;
      await saveConfig(file, bundle);
    }
  }
  return config;
}

export async function pair(label, local = false) {
  const config = await loadCollector();
  if (local && await optionalConfig('watcher.json')) {
    const existing = await readConfig('watcher.json');
    if (!config.reporters.some(row => row.id === existing.reporterId && row.legacy)) {
      throw new Error('This watcher is paired elsewhere; use collector-only mode or a separate installation directory');
    }
    return path.join(dataDir, 'watcher.json');
  }
  if (local && config.reporters.some(row => row.legacy)) throw new Error('Local pairing exists but watcher profile is missing; restore it before continuing');
  await protectData();
  const token = randomBytes(32).toString('hex'), reporterId = randomUUID();
  const address = local ? '127.0.0.1' : config.bindAddress;
  const pairing = validatePairing({ version: 1, reporterId, label,
    collectorUrl: `https://${address.includes(':') ? `[${address}]` : address}:${config.port}`,
    token, certificate: await readFile(path.join(dataDir, 'collector-cert.pem'), 'utf8') });
  config.reporters.push({ id: reporterId, label, tokenHash: digest(token), legacy: local });
  validateCollector(config);
  const file = local ? 'watcher.json' : `pairing-${reporterId}.json`;
  await saveConfig(file, pairing);
  await saveConfig('collector.json', config);
  return path.join(dataDir, file);
}

export async function ensureLocalReporter(config) {
  let local = config.reporters.find(row => row.legacy);
  if (!local) {
    local = { id: randomUUID(), label: os.hostname(), tokenHash: digest(randomBytes(32).toString('hex')), legacy: true };
    config.reporters.push(local);
    validateCollector(config);
    await saveConfig('collector.json', config);
  }
  return local.id;
}

export async function pairConnectionString(label) {
  const file = await pair(label);
  const pairing = await readConfig(path.basename(file));
  return encodeConnectionString(pairing);
}

export async function migrateLegacy(config, collectorFile) {
  try { await stat(collectorFile); return; } catch (error) { if (error.code !== 'ENOENT') throw error; }
  const local = config.reporters.find(row => row.legacy);
  const legacy = await optionalConfig('sessions.json');
  if (!legacy) return;
  if (!local) throw new Error('Existing single-machine state requires a local reporter identity; run "node src/configuration.mjs local" once, or start the collector with self-observation enabled (the default)');
  const backup = path.join(dataDir, `backup-${new Date().toISOString().replace(/[:.]/g, '-')}`);
  await mkdir(backup);
  for (const file of ['sessions.json', 'notifications.json']) {
    try { await copyFile(path.join(dataDir, file), path.join(backup, file)); }
    catch (error) { if (error.code !== 'ENOENT') throw error; }
  }
  if (!await optionalConfig('watcher-sessions.json')) await saveConfig('watcher-sessions.json', legacy);
  console.log('Backed up legacy monitor metadata before migration.');
}

export async function configurationCommand(args) {
  const [command, first, second, third] = args;
  if (command === 'initialize') {
    await initialize(first ?? '127.0.0.1', Number(second ?? 43188), third === 'replace');
    console.log('Collector configured. No firewall or certificate-store changes were made.');
  } else if (command === 'local') {
    await initialize();
    await pair(os.hostname(), true);
    console.log('Local watcher pairing ready.');
  } else if (command === 'pair') {
    console.log(`Private pairing file: ${await pair(first)}`);
    console.log(`Certificate SHA256: ${new X509Certificate(await readFile(path.join(dataDir, 'collector-cert.pem'))).fingerprint256}`);
    console.log('Transfer privately to the intended watcher. Do not share or commit this file.');
  } else if (command === 'import') {
    if (await optionalConfig('watcher-runtime.json')) throw new Error('Stop the watcher before importing a pairing');
    const pairing = validatePairing(JSON.parse(await readFile(path.resolve(first), 'utf8')));
    const previous = await optionalConfig('watcher.json');
    if (previous && previous.reporterId !== pairing.reporterId) throw new Error('This checkout already has a different watcher identity; use a separate installation directory');
    await protectData();
    await saveConfig('watcher.json', pairing);
    console.log(`Imported pairing for ${pairing.label}. Certificate SHA256: ${new X509Certificate(pairing.certificate).fingerprint256}`);
  } else if (command === 'revoke') {
    const config = await loadCollector();
    if (!config.reporters.some(row => row.id === first && !row.legacy)) throw new Error('Unknown remote reporter');
    config.reporters = config.reporters.filter(row => row.id !== first);
    await saveConfig('collector.json', config);
    console.log('Reporter credential revoked; retained metadata is preserved.');
  } else throw new Error('Expected initialize, local, pair, import, or revoke');
}
if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try { await configurationCommand(process.argv.slice(2)); }
  catch (error) { console.error(`Configuration failed: ${error.message}`); process.exitCode = 1; }
}
