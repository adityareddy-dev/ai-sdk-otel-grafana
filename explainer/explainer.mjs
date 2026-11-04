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
import { chromium } from 'playwright';
import { readdirSync, readFileSync } from 'node:fs';
import { z } from 'zod';

const SITE = process.env.INP_SITE ?? 'https://reactinpblame.com/';
// provider/model@pause, the same format as the on-call agent. The default pause
// keeps Gemini 3.1 Flash-Lite inside its free tier's daily requests.
const [modelSpec, pause] = (process.env.INP_MODEL ?? 'google/gemini-3.1-flash-lite@480000').split('@');
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
  readers: [new PeriodicExportingMetricReader({ exporter: new OTLPMetricExporter(), exportIntervalMillis: 15000 })],
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
  advice: { explicitBucketBoundaries: [1, 4, 16, 64, 256, 1024, 4096, 16384, 65536, 262144, 1048576] },
});
const runs = meter.createCounter('inp_explainer.runs', { description: 'Explanations by outcome.', unit: '{run}' });
const checks = meter.createCounter('inp_explainer.checks', {
  description: 'Per-run checks of whether the explanation named the culprit and the fix the demo documents.',
  unit: '{check}',
});

// The demo's slow interactions. `fix` matches the fix the demo's own lab page
// documents for each one, which the model never sees. The lab's cascading-effect
// scenario is left out: on the live production build its click stays under the
// report threshold and the heavy render comes after the paint.
const SCENARIOS = [
  { key: 'context-storm', act: 'click', fix: /usememo|split|separate (the )?context|two contexts|stable (value|actions)/ },
  { key: 'layout-thrash', act: 'click', fix: /batch|reads? (first|before)|before (the )?writes?|css|requestanimationframe|drop the (measure|read)|avoid (reading|measuring)/ },
  { key: 'handler-hog', act: 'click', fix: /worker|scheduler\.yield|yield|defer|settimeout|requestidlecallback|chunk|after (the )?paint/ },
  { key: 'big-list', act: 'type', fix: /virtuali[sz]|usedeferredvalue|usetransition|windowing|react-window/ },
  { key: 'lifted-state', act: 'type', fix: /move (the )?state|state down|coloca|memo/ },
  { key: 'slow-render', act: 'click', fix: /memo|cache|precompute|off the render|worker|store the result/ },
  { key: 'restyle-storm', act: 'click', fix: /virtuali[sz]|content-visibility|contain|fewer cells/ },
];

// The scenario sources from the react-inp-blame repository, so the tool hands
// the model the code that actually ran.
const SOURCES = Object.fromEntries(
  readdirSync(new URL('./source/', import.meta.url)).map((f) => [f, readFileSync(new URL(`./source/${f}`, import.meta.url), 'utf8')]),
);
const tools = {
  get_component_source: tool({
    description: 'The source file that defines a React component (or helper) in the page, by name.',
    inputSchema: z.object({ name: z.string().describe('Component or function name, for example OrderSummary') }),
    execute: async ({ name }) => {
      const bare = name.replace(/[^A-Za-z0-9_]/g, '');
      const re = new RegExp(`(function|const)\\s+${bare}\\b`);
      const hit = Object.entries(SOURCES).find(([, src]) => re.test(src));
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
  if (!browser?.isConnected()) browser = await chromium.launch();
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
  if (!page || Date.now() - loadedAt > 3600_000) await openSite();
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
  await context.with(trace.setSpan(context.active(), span), async () => {
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
      runs.add(1, { ...attrs, 'agent.task': s.key, outcome: 'no_report' });
      page = null;
      return;
    } finally {
      m.end();
    }
    interaction.record(report.duration / 1000, { 'agent.task': s.key, 'react_inp_blame.blame.kind': report.explanation.blame.kind });
    span.setAttributes({ 'react_inp_blame.duration_ms': report.duration, 'react_inp_blame.blame.kind': report.explanation.blame.kind });

    const agent = new ToolLoopAgent({
      model: google(modelId),
      instructions:
        'You help a React developer fix a slow interaction. You get the report react-inp-blame wrote for it. ' +
        'Read the source of the component the report blames with get_component_source before you answer. ' +
        'Then say in at most three sentences which component is slow and why, and the one code change that fixes it. Name the component.',
      tools,
      stopWhen: isStepCount(4),
      maxOutputTokens: 400,
      telemetry: { functionId: 'react-inp-blame-explainer', recordInputs: true, recordOutputs: true },
    });
    const called = [];
    let n = 0;
    try {
      const result = await agent.generate({
        prompt: JSON.stringify(brief(report)),
        abortSignal: AbortSignal.timeout(90_000),
        onStepFinish: (step) => {
          n += 1;
          const u = step.usage ?? {};
          if (u.inputTokens != null) tokenUsage.record(u.inputTokens, { ...attrs, 'agent.task': s.key, 'gen_ai.token.type': 'input' });
          if (u.outputTokens != null) tokenUsage.record(u.outputTokens, { ...attrs, 'agent.task': s.key, 'gen_ai.token.type': 'output' });
          for (const c of step.toolCalls ?? []) called.push(c.toolName);
        },
      });
      const text = (result.text ?? '').toLowerCase();
      const culprits = [report.explanation.blame.name, report.target?.component, report.commits?.[0]?.components?.[0]?.name]
        .filter(Boolean).map((x) => x.toLowerCase());
      const failed = [];
      const check = (name, pass) => {
        checks.add(1, { ...attrs, 'agent.task': s.key, check: name, result: pass ? 'pass' : 'fail' });
        if (!pass) failed.push(name);
      };
      check('read_source', called.includes('get_component_source'));
      check('named_culprit', culprits.some((c) => text.includes(c)));
      check('right_fix', s.fix.test(text));
      runs.add(1, { ...attrs, 'agent.task': s.key, outcome: 'ok' });
      span.setAttributes({ 'agent.outcome': 'ok', 'agent.run.steps': n });
      if (failed.length) span.setAttribute('agent.checks.failed', failed.join(','));
    } catch (e) {
      const outcome = e?.name === 'AbortError' || e?.name === 'TimeoutError' ? 'timeout' : 'error';
      runs.add(1, { ...attrs, 'agent.task': s.key, outcome });
      span.setAttribute('agent.outcome', outcome);
      span.setStatus({ code: SpanStatusCode.ERROR, message: e?.name });
    }
  });
  span.end();
}

// Start every counter series at zero, so Prometheus can take an increase from the first run.
for (const s of SCENARIOS) {
  for (const o of ['ok', 'timeout', 'error', 'no_report']) runs.add(0, { ...attrs, 'agent.task': s.key, outcome: o });
  for (const c of ['read_source', 'named_culprit', 'right_fix']) for (const r of ['pass', 'fail']) {
    checks.add(0, { ...attrs, 'agent.task': s.key, check: c, result: r });
  }
}

let stopping = false;
// Let the zero counts above reach the first export before the first run adds to them.
await new Promise((r) => setTimeout(r, 20_000));
process.on('SIGTERM', async () => {
  stopping = true;
  await browser?.close().catch(() => {});
  await Promise.all([meterProvider.shutdown(), tracerProvider.shutdown()]);
  process.exit(0);
});
while (!stopping) {
  await runOnce().catch((e) => console.error('run failed', e?.message));
  await new Promise((r) => setTimeout(r, PAUSE_MS * (0.5 + Math.random())));
}
