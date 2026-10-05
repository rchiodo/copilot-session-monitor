import { request, HASH, validatePairing, validateReport } from './protocol.mjs';

export class Reporter {
  constructor(pairing, identity) {
    this.pairing = validatePairing(pairing);
    this.identity = identity;
    this.lease = null;
    this.seq = 0;
  }
  async connect() {
    const response = await request(this.pairing, '/v1/connect', {
      version: 1, reporterId: this.pairing.reporterId, ...this.identity,
    });
    if (response.version !== 1 || !HASH.test(response.lease)) throw new Error('Invalid collector handshake');
    this.lease = response.lease;
    this.seq = 0;
  }
  async send(snapshot) {
    const report = validateReport({ version: 1, reporterId: this.pairing.reporterId,
      lease: this.lease, seq: this.seq + 1, sentAt: new Date().toISOString(), ...snapshot });
    try {
      let response;
      try { response = await request(this.pairing, '/v1/report', report); }
      catch (error) {
        if (error.status !== 503) throw error;
        response = await request(this.pairing, '/v1/report', report);
      }
      if (response.seq !== report.seq) throw new Error('Unexpected collector acknowledgement');
      this.seq = report.seq;
      return response;
    } catch (error) {
      this.lease = null;
      throw error;
    }
  }
  async disconnect() {
    if (!this.lease) return;
    const lease = this.lease;
    this.lease = null;
    await request(this.pairing, '/v1/disconnect', { lease });
  }
}
