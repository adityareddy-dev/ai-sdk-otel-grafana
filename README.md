# ai-sdk-otel-grafana

A React interaction explainer measured with the AI SDK's OpenTelemetry histogram.

Chrome opens [reactinpblame.com](https://reactinpblame.com), makes a slow interaction and reads the actual react-inp-blame report. A model reads a pinned source snapshot and suggests a change. This is a demo workload.

The dashboard starts with recent measured attempts, the fraction of whole runs passing evaluation, completed response count and freshness. Failed checks, timeouts, errors, no-report attempts and quota skips have separate outcomes. The browser measurement, SDK model calls, source tool and whole run belong to the same trace.

A passing evaluation establishes four things: a successful tool response contains the blamed component or handler, the structured answer names exactly what the report blamed, its source citation matches that response, and its recommended strategy ID matches a documented option for that demo scenario. It does not validate every statement in the explanation. Suggested changes have not been applied or benchmarked.

The source snapshot is pinned at `ea73e5d`. It is not checked against the current deployed bundle. A painting report without a named culprit cannot pass the exact-name check. `explainer/source/README.md` records the snapshot origin.

`@ai-sdk/otel` records `gen_ai.client.inference.duration`, a histogram of every model call, including failures. I asked for it in [vercel/ai#24229](https://github.com/vercel/ai/issues/24229) and it shipped in 1.0.160.

The pretend-shop agent and its two-model comparison are reference material in `dashboard/reference.json`. Start that workload explicitly with the `reference` Compose profile.

## What it records

| Metric | From | Labels that matter |
| --- | --- | --- |
| `gen_ai.client.inference.duration` (s) | `@ai-sdk/otel`, every model call | `gen_ai.request.model`, `gen_ai.response.model`, `gen_ai.provider.name`, `error.type` |
| `gen_ai.client.token.usage` ({token}) | `explainer.mjs`, per SDK step | `gen_ai.token.type` input or output |
| `inp_explainer.runs` | `explainer.mjs` | `agent.task`, `outcome` |
| `inp_explainer.checks` | `explainer.mjs` | `check`, `result`, `agent.task` |
| `inp_explainer.interaction.duration` | the browser report | `agent.task`, blame kind |
| `inp_explainer.interaction.last_duration`, `last_measured` | latest browser report per scenario | `agent.task` |
| `inp_explainer.last_completed`, `heartbeat` | response and exporter timestamps | no custom labels |
| `traces_span_metrics_duration` (s) | the Collector's `spanmetrics` connector, from the AI SDK's spans | `gen_ai.operation.name` invoke_agent, chat or execute_tool, `gen_ai.request.model`, `gen_ai.tool.name` |

Token usage is recorded by the explainer because the AI SDK puts usage on spans, not on a metric.

The spans go to the same OTLP endpoint, and the Collector's `spanmetrics` connector turns them into duration histograms. That gives a p95 for a whole run and for each tool, which the inference histogram can't, cause it only times the model call. `@ai-sdk/otel` bundles its own copy of `@opentelemetry/api`, so `explainer.mjs` passes its tracer in directly instead of relying on the global one.

The source tool reads the checked-in demo snapshot. `inp_explainer.runs` distinguishes `passed`, `evaluation_failed`, `error`, `timeout`, `no_report` and `quota_blocked`. `inp_explainer.checks` records each completed response's four checks. Last-completed and exporter heartbeat gauges let the dashboard distinguish a stopped service from a low passing fraction.

## The dashboard

[Public dashboard](https://brightparlor286.grafana.net/public-dashboards/5277cad1e14448b58d05284a54404e86). The current dashboard definition was imported through the signed-in Grafana browser on November 7, 2026.

`dashboard/dashboard.json` is the explainer view. `dashboard/reference.json` holds the synthetic shop. Run `python3 dashboard/build.py` to rebuild both. `python3 dashboard/build.py <prometheus-uid> <tempo-uid>` generates `.live.json` versions bound to the existing data sources.

Counts and evaluation use the last hour. The browser table gives cumulative histogram sample counts, latest measured durations and report age per scenario, alongside six-hour run estimates. A fixed, dated model answer and its trace ID appear above the live statistics. Its structured evaluation does not validate every claim in its prose. Prometheus `increase` extrapolates counter samples to the window, so displayed counts can be fractional or rounded. Missing responses stay empty. The answer panel says No data until a response exists, Stale if the latest response is over 20 minutes old or telemetry is over 3 minutes old, and Needs attention when a recent attempt fails any check. Individual check fractions use completed responses; the whole-run fraction includes errors and timeouts in its denominator.

## Run it

```sh
cp .env.example .env
# Supply the existing stack's OTLP endpoint/auth and a Gemini key from a free project.
docker compose up -d
```

The explainer and collector retain Compose restart supervision. `explainer-state` preserves request reservations across restarts; run one explainer instance per state volume. Four provider requests are reserved before each model run and SDK retries are disabled. The cap is shared by one explainer instance, not by unrelated users of the Google project. A stopped or failed run keeps its reservation. The default is 400 reserved requests per Pacific day, at most 100 model runs; the fixed fifteen-minute pause paces eligible runs. On November 7, 2026, AI Studio showed 500 requests per day, 15 per minute and 250,000 input tokens per minute for Gemini 3.1 Flash-Lite on the existing Golden Grot free project. Local quota skips remain visible. Read the project's actual RPM, TPM and RPD in [AI Studio](https://aistudio.google.com/rate-limit) before setting `INP_REQUESTS_PER_DAY`. There is no paid fallback or automatic upgrade.

[Google's pricing](https://ai.google.dev/gemini-api/docs/pricing#gemini-3.1-flash-lite) lists Gemini 3.1 Flash-Lite standard input and output as free on the free tier. [Rate limits](https://ai.google.dev/gemini-api/docs/rate-limits) apply per project and reset at midnight Pacific; available limits are shown in AI Studio rather than guaranteed by the pause. Confirm that billing is disabled for the selected project before running.

Set `INP_EXAMPLE_FILE=/state/example.json` to capture the latest structured demo response for an inspected, dated example. This file contains no prompt text. Metric export and spanmetrics flush use 60 seconds. Spanmetrics has five explicit buckets, operation, model and tool dimensions, and the connector's standard dimensions. Scenario and outcome remain on counters and trace attributes, avoiding a separate histogram for each outcome of each scenario. Token histograms have four explicit buckets and input/output labels. The SDK model-call histogram is preserved.

```sh
cd explainer
npm ci
npm test
cd ..
python3 -m unittest discover -s dashboard -p 'test_*.py'
```

The lab's cascading-effect scenario stays excluded: its render follows the measured paint. A browser interaction below the reporting threshold is recorded as `no_report`, never a successful explanation.
