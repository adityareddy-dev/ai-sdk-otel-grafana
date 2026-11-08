import { test } from 'node:test';
import assert from 'node:assert/strict';
import { evaluate, explain } from './evaluate.mjs';

const report = { explanation: { blame: { kind: 'render', name: 'OrderSummary' } },
  target: { component: 'ContextStorm' }, commits: [{ components: [{ name: 'LineItem' }] }] };
const source = { toolName: 'get_component_source', output: {
  file: 'ContextStorm.tsx', source: 'const OrderSummary = memo(function OrderSummary() {});' } };
const answer = { culprit: 'OrderSummary', source_file: 'ContextStorm.tsx',
  suggested_strategy: 'split_context', explanation: 'Split the context consumed by OrderSummary.' };
const check = (a = answer, results = [source], r = report) =>
  evaluate(r, ['split_context'], JSON.stringify(a), results);

test('a successful source response and exact blamed name pass together', () => {
  assert.equal(check().outcome, 'passed');
});

test('calling the source tool with an error is an evaluation failure', () => {
  const result = check(answer, [{ toolName: 'get_component_source', output: { error: 'Not found' } }]);
  assert.equal(result.checks.read_source, false);
  assert.equal(result.outcome, 'evaluation_failed');
});

test('the target and the first rendered child do not substitute for the blame', () => {
  for (const culprit of ['ContextStorm', 'LineItem']) {
    assert.equal(check({ ...answer, culprit }).checks.named_culprit, false);
  }
});

test('negated prose cannot pass the structured strategy check', () => {
  const result = evaluate(report, ['split_context'], 'Do not split the context. OrderSummary is fine.', [source]);
  assert.equal(result.checks.documented_strategy, false);
  assert.equal(check({ ...answer, suggested_strategy: 'worker' }).checks.documented_strategy, false);
});

test('strategy matching establishes only the selected ID, not prose correctness', () => {
  const result = check({ ...answer, explanation: 'Do not split the context.' });
  assert.equal(result.checks.documented_strategy, true);
});

test('the citation must name the successfully read source containing the culprit', () => {
  assert.equal(check({ ...answer, source_file: 'Other.tsx' }).checks.cited_source, false);
  assert.equal(check(answer, [{ ...source, output: { ...source.output, source: 'function LineItem() {}' } }]).checks.read_source, false);
});

test('a script report can blame a JSX event prop instead of a declared function', () => {
  const handlerReport = { explanation: { blame: { kind: 'script', name: 'onClick', detail: 'HandlerHog' } },
    target: { component: 'HandlerHog', handler: 'onClick' } };
  const handlerSource = { toolName: 'get_component_source', output: {
    file: 'HandlerHog.tsx', source: 'function HandlerHog() { return <button onClick={computeChecksum} />; }' } };
  const result = evaluate(handlerReport, ['worker'], JSON.stringify({ ...answer, culprit: 'onClick',
    source_file: 'HandlerHog.tsx', suggested_strategy: 'worker' }), [handlerSource]);
  assert.equal(result.outcome, 'passed');
});

test('an unnamed painting blame cannot be replaced by the target component', () => {
  const result = check({ ...answer, culprit: 'RestyleStorm' }, [source], {
    explanation: { blame: { kind: 'painting', name: null } }, target: { component: 'RestyleStorm' } });
  assert.equal(result.checks.named_culprit, false);
});

test('finishing an answer differs from passing evaluation', async () => {
  const checks = [];
  const result = await explain({ report, strategies: ['split_context'], onCheck: (...c) => checks.push(c),
    generate: async ({ onStepFinish }) => {
      onStepFinish({ toolResults: [{ toolName: 'get_component_source', output: { error: 'Not found' } }] });
      return { text: JSON.stringify(answer) };
    } });
  assert.equal(result.completed, true);
  assert.equal(result.outcome, 'evaluation_failed');
  assert.equal(checks.length, 4);
});

test('a deadline abort remains a timeout without manufacturing an evaluation', async () => {
  const result = await explain({ report, strategies: ['split_context'], signal: AbortSignal.timeout(5),
    onCheck: () => assert.fail('No finished response'),
    generate: ({ abortSignal }) => new Promise((resolve, reject) => {
      abortSignal.addEventListener('abort', () => reject(abortSignal.reason), { once: true });
      setTimeout(resolve, 50);
    }) });
  assert.deepEqual(result, { outcome: 'timeout', completed: false });
});

test('provider errors stay distinct from deadlines', async () => {
  const result = await explain({ report, strategies: [], onCheck: () => assert.fail(),
    generate: async () => { throw new Error('Provider unavailable'); } });
  assert.deepEqual(result, { outcome: 'error', completed: false });
});
