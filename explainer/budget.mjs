import { readFileSync, writeFileSync, renameSync, mkdirSync } from 'node:fs';
import { dirname } from 'node:path';

export class DailyBudget {
  constructor(file, limit, now = () => new Date()) {
    if (!Number.isInteger(limit) || limit < 1) throw new Error('INP_REQUESTS_PER_DAY must be a positive integer');
    this.file = file;
    this.limit = limit;
    this.now = now;
  }

  reserve(requests) {
    const day = new Intl.DateTimeFormat('en-CA', { timeZone: 'America/Los_Angeles' }).format(this.now());
    let state;
    try { state = JSON.parse(readFileSync(this.file, 'utf8')); } catch (error) {
      if (error.code !== 'ENOENT') throw error;
      state = { day, used: 0 };
    }
    if (typeof state.day !== 'string' || !Number.isInteger(state.used) || state.used < 0) throw new Error('Invalid request budget state');
    if (state.day !== day) state = { day, used: 0 };
    if (state.used + requests > this.limit) return false;
    state.used += requests;
    mkdirSync(dirname(this.file), { recursive: true });
    writeFileSync(`${this.file}.tmp`, JSON.stringify(state), { mode: 0o600 });
    renameSync(`${this.file}.tmp`, this.file);
    return true;
  }
}
