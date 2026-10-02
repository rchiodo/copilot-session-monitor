export class MonitorActions {
  constructor(engine, refresh, persist, healthy) {
    Object.assign(this, { engine, refresh, persist, healthy });
    this.queue = Promise.resolve();
  }

  run(action) {
    const result = this.queue.then(action);
    this.queue = result.then(() => undefined, () => undefined);
    return result;
  }

  dismiss(entries) {
    return this.run(async () => {
      await this.refresh();
      if (!this.healthy()) throw Object.assign(new Error('Observer unavailable; nothing dismissed'), { status: 503 });
      const before = new Map(this.engine.dismissed);
      try {
        const result = this.engine.dismiss(entries);
        await this.persist();
        return result;
      } catch (error) {
        this.engine.dismissed = before;
        if (error instanceof TypeError) error.status = 400;
        throw error;
      }
    });
  }
}

export async function readDismissEntries(request) {
  let size = 0;
  const chunks = [];
  for await (const chunk of request) {
    size += chunk.length;
    if (size > 1048576) throw Object.assign(new Error('Dismiss request too large'), { status: 413 });
    chunks.push(chunk);
  }
  try {
    return JSON.parse(Buffer.concat(chunks).toString('utf8')).entries;
  } catch {
    throw Object.assign(new Error('Invalid dismiss request JSON'), { status: 400 });
  }
}
