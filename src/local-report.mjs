import os from 'node:os';
import path from 'node:path';
import { LocalSource } from './source.mjs';
import { FamilyMonitor } from './families.mjs';
import { observedMetadata, digest } from './protocol.mjs';

// Shared local Copilot-session observation logic used both by the standalone
// watcher (which reports over HTTPS to a paired collector) and the collector's
// own in-process self-source (which observes its own machine with no network
// hop at all). Keeping this in one place means both call sites apply the exact
// same conservative completion rules.
export function createLocalObserver(label, retained, processSnapshot, onNotice) {
  const monitor = new FamilyMonitor(label, async (key, alert) => {
    onNotice({ key: digest(key), familyId: alert.familyId, kind: alert.kind });
  }, retained);
  const source = new LocalSource(path.join(os.homedir(), '.copilot'), processSnapshot);
  for (const id of monitor.rows.keys()) source.tracked.add(id);
  return { monitor, source };
}

export async function pollLocal({ source, monitor }, forcedGapReason) {
  let result, issues = [], healthy = true, samples = [];
  try {
    if (forcedGapReason) throw new Error(forcedGapReason);
    const observed = await source.poll();
    samples = observed.samples;
    result = await monitor.update(observed.samples, { relatives: observed.relatives });
    issues = observed.issues;
    healthy = !result.gap;
    for (const id of monitor.rows.keys()) source.tracked.add(id);
    source.releaseIdle(new Set(monitor.rows.keys()));
  } catch (error) {
    healthy = false;
    issues = [`Local observation unavailable (${error.code ?? error.name}); no completion inferred`];
    result = await monitor.update([], { healthy: false, reason: issues[0] });
  }
  return { result, issues, healthy, samples };
}

export function localReportPayload(monitor, { result, issues, healthy, samples }, notices) {
  return { ...observedMetadata({ ...result, relatives: monitor.relatives }, samples, monitor.observed), healthy, issues, notices };
}
