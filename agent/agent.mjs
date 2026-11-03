// An on-call helper agent built on the AI SDK. It answers questions about a
// fake shop using two tools, on models from the Gemini API (or a local Ollama
// model), and sends its metrics over OTLP. Latency comes from @ai-sdk/otel's
// gen_ai.client.inference.duration histogram. Token usage is recorded here as
// the semconv gen_ai.client.token.usage histogram, because the AI SDK puts
// usage on spans, not on a metric.
import { context, metrics, trace, SpanStatusCode } from '@opentelemetry/api';
import { TracerProvider, BatchSpanProcessor } from '@opentelemetry/sdk-trace';
import { AsyncLocalStorageContextManager } from '@opentelemetry/context-async-hooks';
import { OTLPTraceExporter } from '@opentelemetry/exporter-trace-otlp-http';
import { MeterProvider, PeriodicExportingMetricReader } from '@opentelemetry/sdk-metrics';
import { OTLPMetricExporter } from '@opentelemetry/exporter-metrics-otlp-http';
import { resourceFromAttributes } from '@opentelemetry/resources';
import { OpenTelemetry } from '@ai-sdk/otel';
import { registerTelemetry, ToolLoopAgent, isStepCount, tool } from 'ai';
import { createOpenAICompatible } from '@ai-sdk/openai-compatible';
import { google } from '@ai-sdk/google';
import { z } from 'zod';

const OLLAMA = process.env.OLLAMA_URL ?? 'http://localhost:11434/v1';
// provider/model@pause entries. Each model runs its own loop at its own pace,
// so a free tier's daily request limit can be kept, for example
// ollama/qwen2.5:0.5b@30000,google/gemini-3.5-flash-lite@240000
const MODELS = (process.env.MODELS ?? 'ollama/qwen2.5:0.5b@30000').split(',').map((m) => {
  const [id, pause] = m.split('@');
  const i = id.indexOf('/');
  return { providerId: id.slice(0, i), modelId: id.slice(i + 1), pauseMs: Number(pause ?? 30000) };
});

const resource = resourceFromAttributes({
  'service.name': 'oncall-agent',
  'service.namespace': 'golden-grot-demo',
  'service.version': '1.0.0',
});
// The AI SDK's spans (invoke_agent, chat, execute_tool) go to the same OTLP endpoint.
const tracerProvider = new TracerProvider({
  resource,
  spanProcessors: [new BatchSpanProcessor({ exporter: new OTLPTraceExporter() })],
});
context.setGlobalContextManager(new AsyncLocalStorageContextManager().enable());
trace.setGlobalTracerProvider(tracerProvider);

const provider = new MeterProvider({
  resource,
  readers: [
    new PeriodicExportingMetricReader({
      exporter: new OTLPMetricExporter(),
      exportIntervalMillis: 15000,
    }),
  ],
});
metrics.setGlobalMeterProvider(provider);
// @ai-sdk/otel ships its own copy of @opentelemetry/api, so hand it the tracer directly.
registerTelemetry(new OpenTelemetry({ tracer: tracerProvider.getTracer('gen_ai') }));
// One span per question, above the AI SDK's invoke_agent span, carrying what the
// metrics can't: which question it was, how it ended and which checks it failed.
const tracer = tracerProvider.getTracer('oncall-agent', '1.0.0');

const meter = metrics.getMeter('oncall-agent', '1.0.0');
// Same buckets the semconv spec advises for gen_ai.client.token.usage.
const tokenUsage = meter.createHistogram('gen_ai.client.token.usage', {
  description: 'Number of input and output tokens used.',
  unit: '{token}',
  advice: {
    explicitBucketBoundaries: [1, 4, 16, 64, 256, 1024, 4096, 16384, 65536, 262144, 1048576],
  },
});
const runs = meter.createCounter('agent.runs', { description: 'Agent runs by outcome.', unit: '{run}' });
const steps = meter.createHistogram('agent.run.steps', {
  description: 'Model calls per agent run.',
  unit: '{step}',
  advice: { explicitBucketBoundaries: [1, 2, 3, 4, 5, 6] },
});
const toolCalls = meter.createCounter('agent.tool.calls', { description: 'Tool calls the model asked for.', unit: '{call}' });

const ollama = createOpenAICompatible({ name: 'ollama', baseURL: OLLAMA });
const models = { ollama, google };
// Same names @ai-sdk/otel puts on the inference histogram, so the two metrics join.
const providerName = (p) => (p.startsWith('google') ? 'gcp.gemini' : p);

const SERVICES = ['checkout', 'cart', 'search', 'payments', 'auth'];
const tools = {
  get_error_rate: tool({
    description: 'Current error rate of a service, as a percentage of requests in the last 5 minutes.',
    inputSchema: z.object({ service: z.string().describe('Service name, for example checkout') }),
    execute: async ({ service }) => {
      await new Promise((r) => setTimeout(r, 50 + Math.random() * 200));
      const base = SERVICES.indexOf(service.toLowerCase()) >= 0 ? 0.2 : 0;
      return { service, error_rate_percent: +(base + Math.random() * 4).toFixed(2) };
    },
  }),
  get_runbook: tool({
    description: 'The first step of the runbook for a service when its error rate is high.',
    inputSchema: z.object({ service: z.string() }),
    execute: async ({ service }) => ({
      service,
      first_step: `Check the last deploy of ${service} and roll back if errors started after it.`,
    }),
  }),
};

// Each question says which tools a correct run must call and which words the
// answer must contain. The checks are what "doing what it was asked" means here.
const QUESTIONS = [
  { task: 'health_check', prompt: 'Is checkout healthy right now? Use the tools.', tools: ['get_error_rate'], words: ['checkout'] },
  { task: 'triage', prompt: 'What is the error rate of payments, and what should I do if it is above 2 percent?', tools: ['get_error_rate', 'get_runbook'], words: ['payments'] },
  { task: 'compare', prompt: 'Compare the error rates of cart and search.', tools: ['get_error_rate'], words: ['cart', 'search'] },
  { task: 'status', prompt: 'Give me a one line status of auth.', tools: ['get_error_rate'], words: ['auth'] },
  { task: 'runbook', prompt: 'Search is paging. What is the first runbook step?', tools: ['get_runbook'], words: ['deploy'] },
  { task: 'explain', prompt: 'Summarize in one sentence what an error budget is.', tools: [], words: ['error'] },
];
const checks = meter.createCounter('agent.task.checks', {
  description: 'Per-run checks of whether the agent did what the question asked.',
  unit: '{check}',
});

const pick = (a) => a[Math.floor(Math.random() * a.length)];

async function runOnce({ providerId, modelId }) {
  const model = models[providerId](modelId);
  const agent = new ToolLoopAgent({
    model,
    instructions: 'You are an on-call helper. Use the tools when a question names a service. Answer in two sentences or fewer.',
    tools,
    stopWhen: isStepCount(4),
    maxOutputTokens: 200,
    telemetry: { functionId: 'oncall-agent', recordInputs: false, recordOutputs: false },
  });
  const attrs = { 'gen_ai.operation.name': 'chat', 'gen_ai.provider.name': providerName(model.provider), 'gen_ai.request.model': modelId };
  // One run in ten gets a tight deadline, so the dashboard shows what timeouts look like.
  const tight = Math.random() < 0.1;
  const q = pick(QUESTIONS);
  const called = new Set();
  let n = 0;
  const span = tracer.startSpan('oncall_question', { attributes: { 'gen_ai.request.model': modelId, 'agent.task': q.task } });
  try {
    await context.with(trace.setSpan(context.active(), span), async () => {
      const result = await agent.generate({
        prompt: q.prompt,
        abortSignal: AbortSignal.timeout(tight ? 1500 : 90000),
        onStepFinish: (step) => {
          n += 1;
          const u = step.usage ?? {};
          if (u.inputTokens != null) tokenUsage.record(u.inputTokens, { ...attrs, 'gen_ai.token.type': 'input' });
          if (u.outputTokens != null) tokenUsage.record(u.outputTokens, { ...attrs, 'gen_ai.token.type': 'output' });
          for (const c of step.toolCalls ?? []) {
            called.add(c.toolName);
            toolCalls.add(1, { ...attrs, 'gen_ai.tool.name': c.toolName });
          }
        },
      });
      runs.add(1, { ...attrs, outcome: 'ok', finish_reason: result.finishReason ?? 'unknown' });
      const text = (result.text ?? '').toLowerCase();
      const failed = [];
      const check = (name, pass) => {
        checks.add(1, { ...attrs, 'agent.task': q.task, check: name, result: pass ? 'pass' : 'fail' });
        if (!pass) failed.push(name);
      };
      check('used_required_tools', q.tools.every((t) => called.has(t)));
      check('answered', text.trim().length > 0);
      check('on_topic', q.words.every((w) => text.includes(w)));
      span.setAttribute('agent.outcome', 'ok');
      if (failed.length) span.setAttribute('agent.checks.failed', failed.join(','));
    });
  } catch (e) {
    const outcome = e?.name === 'AbortError' || e?.name === 'TimeoutError' ? 'timeout' : 'error';
    runs.add(1, { ...attrs, outcome, finish_reason: 'error' });
    span.setAttribute('agent.outcome', outcome);
    span.setStatus({ code: SpanStatusCode.ERROR, message: e?.name });
  } finally {
    steps.record(Math.max(n, 1), attrs);
    span.end();
  }
}

// Start every counter series at zero. Prometheus can't take an increase from a
// series' first sample, so without this the first run per model would vanish.
for (const { providerId, modelId } of MODELS) {
  const attrs = { 'gen_ai.operation.name': 'chat', 'gen_ai.provider.name': providerName(models[providerId](modelId).provider), 'gen_ai.request.model': modelId };
  runs.add(0, { ...attrs, outcome: 'ok', finish_reason: 'stop' });
  runs.add(0, { ...attrs, outcome: 'timeout', finish_reason: 'error' });
  runs.add(0, { ...attrs, outcome: 'error', finish_reason: 'error' });
  for (const q of QUESTIONS) for (const c of ['used_required_tools', 'answered', 'on_topic']) for (const r of ['pass', 'fail']) {
    checks.add(0, { ...attrs, 'agent.task': q.task, check: c, result: r });
  }
  for (const t of Object.keys(tools)) toolCalls.add(0, { ...attrs, 'gen_ai.tool.name': t });
}

let stopping = false;
process.on('SIGTERM', async () => {
  stopping = true;
  await Promise.all([provider.shutdown(), tracerProvider.shutdown()]);
  process.exit(0);
});
await Promise.all(MODELS.map(async (m) => {
  while (!stopping) {
    await runOnce(m).catch((e) => console.error('run failed', e?.message));
    await new Promise((r) => setTimeout(r, m.pauseMs * (0.5 + Math.random())));
  }
}));
