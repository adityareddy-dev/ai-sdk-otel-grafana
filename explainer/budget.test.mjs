import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { DailyBudget } from './budget.mjs';

test('reservations survive restarts and reset at midnight Pacific', () => {
  const dir = mkdtempSync(`${tmpdir()}/inp-budget-`);
  let now = new Date('2026-11-08T07:59:00Z');
  const budget = () => new DailyBudget(`${dir}/state.json`, 8, () => now);
  try {
    assert.equal(budget().reserve(4), true);
    assert.equal(budget().reserve(4), true);
    assert.equal(budget().reserve(4), false);
    now = new Date('2026-11-08T08:01:00Z');
    assert.equal(budget().reserve(4), true);
    writeFileSync(`${dir}/state.json`, 'broken');
    assert.throws(() => budget().reserve(4));
  } finally { rmSync(dir, { recursive: true }); }
});
