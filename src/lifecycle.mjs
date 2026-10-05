import { open, readFile, unlink } from 'node:fs/promises';
import path from 'node:path';
import { randomUUID } from 'node:crypto';

export async function acquireRole(directory, role) {
  const file = path.join(directory, `${role}.lock`);
  const identity = randomUUID();
  try {
    const previous = JSON.parse(await readFile(file, 'utf8'));
    let alive = true;
    try { process.kill(previous.pid, 0); } catch (error) {
      if (error.code !== 'ESRCH') throw error;
      alive = false;
    }
    if (alive) throw new Error(`${role} already owns this directory (or its old PID was reused); verify the existing process first`);
    await unlink(file);
  } catch (error) { if (error.code !== 'ENOENT') throw error; }
  const handle = await open(file, 'wx', 0o600);
  await handle.writeFile(JSON.stringify({ pid: process.pid, identity }));
  await handle.close();
  return async () => {
    if (JSON.parse(await readFile(file, 'utf8')).identity === identity) await unlink(file);
  };
}
