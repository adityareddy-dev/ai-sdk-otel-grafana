// A fix explainer for react-inp-blame. A headless Chrome opens the live demo at
// reactinpblame.com, makes one of its slow interactions, and reads the report
// the library writes for it. A model on the AI SDK gets that report, can read
// the component's source with one tool, and says why the interaction was slow
// and what to change. Latency comes from @ai-sdk/otel's
// gen_ai.client.inference.duration histogram, like the on-call agent next to it.
import { context, metrics, trace, SpanStatusCode } from '@opentelemetry/api';
import { TracerProvider, BatchSpanProcessor } from '@opentelemetry/sdk-trace';
import { AsyncLocalStorageContextManager } from '@opentelemetry/context-async-hooks';
import { OTLPTraceExporter } from '@opentelemetry/exporter-trace-otlp-http';
import { MeterProvider, PeriodicExportingMetricReader } from '@opentelemetry/sdk-metrics';
import { OTLPMetricExporter } from '@opentelemetry/exporter-metrics-otlp-http';
import { resourceFromAttributes } from '@opentelemetry/resources';
import { OpenTelemetry } from '@ai-sdk/otel';
import { registerTelemetry, ToolLoopAgent, isStepCount, tool } from 'ai';
import { google } from '@ai-sdk/google';
import { declares, explain, STRATEGIES } from './evaluate.mjs';
import { DailyBudget } from './budget.mjs';
import { chromium } from 'playwright';
import { readdirSync, readFileSync, writeFileSync } from 'node:fs';
import { z } from 'zod';

const SITE = process.env.INP_SITE ?? 'https://reactinpblame.com/';
// provider/model@pause, the same format as the on-call agent. The default pause
// controls pacing; the persistent request budget below limits model calls.
const [modelSpec, pause] = (process.env.INP_MODEL ?? 'google/gemini-3.1-flash-lite@900000').split('@');
const modelId = modelSpec.slice(modelSpec.indexOf('/') + 1);
const PAUSE_MS = Number(pause ?? 90000);

const resource = resourceFromAttributes({
  'service.name': 'react-inp-blame-explainer',
  'service.namespace': 'golden-grot-demo',
  'service.version': '1.0.0',
});
const tracerProvider = new TracerProvider({
  resource,
  spanProcessors: [new BatchSpanProcessor({ exporter: new OTLPTraceExporter() })],
});
context.setGlobalContextManager(new AsyncLocalStorageContextManager().enable());
trace.setGlobalTracerProvider(tracerProvider);
const meterProvider = new MeterProvider({
  resource,
  readers: [new PeriodicExportingMetricReader({ exporter: new OTLPMetricExporter(), exportIntervalMillis: 60000 })],
});
metrics.setGlobalMeterProvider(meterProvider);
registerTelemetry(new OpenTelemetry({ tracer: tracerProvider.getTracer('gen_ai') }));
const tracer = tracerProvider.getTracer('react-inp-blame-explainer', '1.0.0');
const meter = metrics.getMeter('react-inp-blame-explainer', '1.0.0');

const interaction = meter.createHistogram('inp_explainer.interaction.duration', {
  description: 'Duration of the interaction react-inp-blame reported, measured in the browser.',
  unit: 's',
  advice: { explicitBucketBoundaries: [0.05, 0.1, 0.2, 0.3, 0.5, 1, 2, 4, 8] },
});
const tokenUsage = meter.createHistogram('gen_ai.client.token.usage', {
  description: 'Number of input and output tokens used.',
  unit: '{token}',
  advice: { explicitBucketBoundaries: [256, 1024, 4096, 16384] },
});
const runs = meter.createCounter('inp_explainer.runs', { description: 'Explanations by outcome.', unit: '{run}' });
const checks = meter.createCounter('inp_explainer.checks', {
  description: 'Source, exact blamed name, citation and structured strategy checks; not a benchmark of the suggestion.',
  unit: '{check}',
});

const SCENARIOS = [
  { key: 'context-storm', act: 'click', strategies: ['stabilize_context', 'split_context'] },
  { key: 'layout-thrash', act: 'click', strategies: ['batch_layout', 'css_layout'] },
  { key: 'handler-hog', act: 'click', strategies: ['worker', 'yield_work'] },
  { key: 'big-list', act: 'type', strategies: ['virtualize', 'defer_render'] },
  { key: 'lifted-state', act: 'type', strategies: ['colocate_state', 'memoize'] },
  { key: 'slow-render', act: 'click', strategies: ['memoize', 'worker'] },
  { key: 'restyle-storm', act: 'click', strategies: ['virtualize', 'contain_layout'] },
];
const CHECKS = ['read_source', 'named_culprit', 'documented_strategy', 'cited_source'];
const OUTCOMES = ['passed', 'evaluation_failed', 'timeout', 'error', 'no_report', 'quota_blocked'];
const REQUESTS_PER_RUN = 4;
const budget = new DailyBudget(process.env.INP_BUDGET_FILE ?? '/state/requests.json',
  Number(process.env.INP_REQUESTS_PER_DAY ?? 400));
let lastFinished = 0;
let lastCompleted = 0;
const latestReports = new Map();
meter.createObservableGauge('inp_explainer.interaction.last_duration', { unit: 's' }).addCallback((r) => {
  for (const [task, report] of latestReports) r.observe(report.duration / 1000, { 'agent.task': task });
});
meter.createObservableGauge('inp_explainer.interaction.last_measured', { unit: 's' }).addCallback((r) => {
  for (const [task, report] of latestReports) r.observe(report.measuredAt, { 'agent.task': task });
});
meter.createObservableGauge('inp_explainer.last_finished', { unit: 's' }).addCallback((r) => {
  if (lastFinished) r.observe(lastFinished);
});
meter.createObservableGauge('inp_explainer.last_completed', { unit: 's' }).addCallback((r) => {
  if (lastCompleted) r.observe(lastCompleted);
});
meter.createObservableGauge('inp_explainer.heartbeat', { unit: 's' }).addCallback((r) => r.observe(Date.now() / 1000));

// The tool reads the demo's pinned source snapshot.
const SOURCES = Object.fromEntries(
  readdirSync(new URL('./source/', import.meta.url)).map((f) => [f, readFileSync(new URL(`./source/${f}`, import.meta.url), 'utf8')]),
);
const tools = {
  get_component_source: tool({
    description: 'The source file that defines a React component (or helper) in the page, by name.',
    inputSchema: z.object({ name: z.string().describe('Component or function name, for example OrderSummary') }),
    execute: async ({ name }) => {
      const hit = Object.entries(SOURCES).find(([, src]) => declares(src, name));
      return hit ? { file: hit[0], source: hit[1] } : { error: `No component named ${name}`, files: Object.keys(SOURCES) };
    },
  }),
};

// The fields of the report a developer reads, without the raw entries.
function brief(r) {
  const c = r.commits?.[0];
  return {
    interaction: r.type,
    duration_ms: Math.round(r.duration),
    rating: r.explanation.rating,
    verdict: r.verdict,
    blame: r.explanation.blame,
    cause: r.explanation.cause,
    notes: r.explanation.notes,
    phases: r.explanation.phases.map((p) => ({ label: p.label, ms: Math.round(p.ms) })),
    target: r.target && { component: r.target.component, handler: r.target.handler, owners: r.target.owners, label: r.target.label },
    render: c && { rendered: c.rendered, hot_path: c.hotPath, top_components: c.components.slice(0, 5).map((x) => ({ name: x.name, count: x.count })) },
    later_renders: r.followUps?.length ?? 0,
    forced_layout_ms: Math.round((r.frames ?? []).reduce((a, f) => a + f.forcedLayout, 0)),
    react_build: r.reactBuild,
  };
}

let browser;
let page;
let loadedAt = 0;
async function openSite() {
  if (!browser?.isConnected()) browser = await chromium.launch(
    process.env.CHROMIUM_EXECUTABLE_PATH ? { executablePath: process.env.CHROMIUM_EXECUTABLE_PATH, args: ['--no-sandbox'] } : {});
  if (page) await page.close().catch(() => {});
  page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  await page.goto(`${SITE}#lab/context-storm`, { waitUntil: 'load' });
  await page.waitForFunction(() => window.__REACT_INP_BLAME__);
  loadedAt = Date.now();
}

// Makes one interaction on the live page and returns react-inp-blame's report.
async function measure(s) {
  // The demo routes on the hash, so after the first load nothing else is fetched.
  // The page is reloaded once an hour to pick up a new deploy.
  if (!page || page.isClosed() || Date.now() - loadedAt > 3600_000) await openSite();
  await page.evaluate((k) => { location.hash = `lab/${k}`; }, s.key);
  const trigger = page.locator('[data-test=trigger]');
  await trigger.waitFor();
  if (s.act === 'type') await trigger.fill('');
  await page.evaluate(() => new Promise((r) => requestIdleCallback(() => r(), { timeout: 2000 })));
  await page.evaluate(() => window.__REACT_INP_BLAME__.clear());
  if (s.act === 'type') {
    await trigger.focus();
    await page.keyboard.type('a');
  } else {
    await trigger.click();
  }
  await page.waitForFunction(() => window.__REACT_INP_BLAME__.last(), null, { timeout: 10_000 });
  // A long animation frame or a later render can join the report just after it is published.
  await page.waitForTimeout(800);
  return page.evaluate(() => window.__REACT_INP_BLAME__.last());
}

const attrs = { 'gen_ai.operation.name': 'chat', 'gen_ai.provider.name': 'gcp.gemini', 'gen_ai.request.model': modelId };
let next = 0;

async function runOnce() {
  const s = SCENARIOS[next++ % SCENARIOS.length];
  const span = tracer.startSpan('explain_interaction', { attributes: { ...attrs, 'agent.task': s.key, 'url.full': `${SITE}#lab/${s.key}` } });
  try { await context.with(trace.setSpan(context.active(), span), async () => {
    let report;
    const m = tracer.startSpan('measure_interaction', { attributes: { 'agent.task': s.key } });
    try {
      report = await measure(s);
      m.setAttributes({
        'react_inp_blame.duration_ms': report.duration,
        'react_inp_blame.blame.kind': report.explanation.blame.kind,
        'react_inp_blame.blame.name': report.explanation.blame.name ?? '',
      });
    } catch (e) {
      m.setStatus({ code: SpanStatusCode.ERROR, message: e?.message });
      span.setAttribute('agent.outcome', 'no_report');
      runs.add(1, { 'agent.task': s.key, outcome: 'no_report' });
      await page?.close().catch(() => {});
      page = null;
      return;
    } finally {
      m.end();
    }
    interaction.record(report.duration / 1000, { 'agent.task': s.key, 'react_inp_blame.blame.kind': report.explanation.blame.kind });
    latestReports.set(s.key, { duration: report.duration, measuredAt: Date.now() / 1000 });
    span.setAttributes({ 'react_inp_blame.duration_ms': report.duration, 'react_inp_blame.blame.kind': report.explanation.blame.kind });

    const agent = new ToolLoopAgent({
      model: google(modelId),
      maxRetries: 0,
      instructions:
        'You help a React developer fix a slow interaction. You get the report react-inp-blame wrote for it. ' +
        'Read the source of the component the report blames with get_component_source before you answer. ' +
        'Return only JSON with culprit (the exact blame.name), source_file (the file you read), ' +
        'suggested_strategy (one of ' + STRATEGIES.join(', ') + '), and explanation (up to three sentences). ' +
        'Choose the strategy you recommend, not one you reject. Explain a proposed change; do not claim it was applied or benchmarked.',
      tools,
      stopWhen: isStepCount(REQUESTS_PER_RUN),
      maxOutputTokens: 400,
      telemetry: { functionId: 'react-inp-blame-explainer', recordInputs: false, recordOutputs: false },
    });
    if (!budget.reserve(REQUESTS_PER_RUN)) {
      runs.add(1, { 'agent.task': s.key, outcome: 'quota_blocked' });
      span.setAttribute('agent.outcome', 'quota_blocked');
      return;
    }
    let n = 0;
    const result = await explain({
      report, strategies: s.strategies, signal: AbortSignal.timeout(90_000),
      onCheck: (check, pass) => checks.add(1, { 'agent.task': s.key, check, result: pass ? 'pass' : 'fail' }),
      generate: async ({ abortSignal, onStepFinish }) => agent.generate({
        prompt: JSON.stringify(brief(report)), abortSignal,
        onStepFinish: (step) => {
          n += 1;
          const u = step.usage ?? {};
          if (u.inputTokens != null) tokenUsage.record(u.inputTokens, { 'gen_ai.token.type': 'input' });
          if (u.outputTokens != null) tokenUsage.record(u.outputTokens, { 'gen_ai.token.type': 'output' });
          onStepFinish(step);
        },
      }),
    });
    if (result.completed) lastCompleted = Date.now() / 1000;
    runs.add(1, { 'agent.task': s.key, outcome: result.outcome });
    span.setAttributes({ 'agent.outcome': result.outcome, 'agent.response.completed': result.completed, 'agent.run.steps': n });
    if (result.answer) span.setAttributes({
      'agent.suggestion.strategy': result.answer.suggested_strategy,
      'agent.suggestion.culprit': result.answer.culprit,
      'agent.suggestion.source_file': result.answer.source_file,
    });
    if (result.answer && process.env.INP_EXAMPLE_FILE) {
      try {
        writeFileSync(process.env.INP_EXAMPLE_FILE, JSON.stringify({
          recorded_at: new Date().toISOString(), scenario: s.key, duration_ms: report.duration,
          blame: report.explanation.blame, answer: result.answer, checks: result.checks,
          trace_id: span.spanContext().traceId, outcome: result.outcome,
        }, null, 2), { mode: 0o600 });
      } catch (error) { console.error('example capture failed', error?.name); }
    }
    if (result.checks) {
      const failed = Object.keys(result.checks).filter((c) => !result.checks[c]);
      if (failed.length) span.setAttribute('agent.checks.failed', failed.join(','));
    }
    if (!result.completed) span.setStatus({ code: SpanStatusCode.ERROR, message: result.outcome });
  }); } catch (error) {
    runs.add(1, { 'agent.task': s.key, outcome: 'error' });
    span.setAttribute('agent.outcome', 'error');
    span.setStatus({ code: SpanStatusCode.ERROR, message: error?.name });
  } finally {
    lastFinished = Date.now() / 1000;
    span.end();
  }
}

// Start every counter series at zero, so Prometheus can take an increase from the first run.
for (const s of SCENARIOS) {
  for (const o of OUTCOMES) runs.add(0, { 'agent.task': s.key, outcome: o });
  for (const c of CHECKS) for (const r of ['pass', 'fail']) {
    checks.add(0, { 'agent.task': s.key, check: c, result: r });
  }
}

let stopping = false;
// Let the zero counts above reach the first export before the first run adds to them.
await new Promise((r) => setTimeout(r, 65_000));
process.on('SIGTERM', async () => {
  stopping = true;
  await browser?.close().catch(() => {});
  await Promise.all([meterProvider.shutdown(), tracerProvider.shutdown()]);
  process.exit(0);
});
while (!stopping) {
  await runOnce().catch((e) => console.error('run failed', e?.name));
  await new Promise((r) => setTimeout(r, PAUSE_MS));
}
