# ai-sdk-otel-grafana

A Grafana dashboard for an AI SDK agent, built on the metric the AI SDK records itself.

`@ai-sdk/otel` records `gen_ai.client.inference.duration`, a histogram of how long each model call took, with the provider, the requested and returned model, and `error.type` when a call fails. I asked for it in [vercel/ai#24229](https://github.com/vercel/ai/issues/24229) and it shipped in `@ai-sdk/otel` 1.0.160. Before that the latency was only on spans, so you couldn't get a p95 per model without a traces backend doing the math.

The agent here is a small on-call helper. It gets questions about a pretend shop ("is checkout healthy", "payments is at 3 percent, what do I do") and has two tools, `get_error_rate` and `get_runbook`. It runs all day on two models through the Gemini API free tier, Gemini 3.5 Flash-Lite and the open-weights Gemma 4 26B, so the dashboard always has two models to compare.

## What it records

| Metric | From | Labels that matter |
| --- | --- | --- |
| `gen_ai.client.inference.duration` (s) | `@ai-sdk/otel`, every model call | `gen_ai.request.model`, `gen_ai.response.model`, `gen_ai.provider.name`, `error.type` |
| `gen_ai.client.token.usage` ({token}) | `agent.mjs`, per step, semconv buckets | `gen_ai.token.type` input or output |
| `agent.runs` | `agent.mjs` | `outcome` ok, timeout or error |
| `agent.task.checks` | `agent.mjs` | `check`, `result`, `agent.task` |
| `agent.tool.calls`, `agent.run.steps` | `agent.mjs` | `gen_ai.tool.name` |
| `traces_span_metrics_duration` (s) | the Collector's `spanmetrics` connector, from the AI SDK's spans | `gen_ai.operation.name` invoke_agent, chat or execute_tool, `gen_ai.request.model`, `gen_ai.tool.name` |

Token usage is recorded in the agent cause the AI SDK puts usage on spans, not on a metric.

The spans go to the same OTLP endpoint, and the Collector's `spanmetrics` connector turns them into duration histograms. That gives a p95 for a whole run and for each tool, which the inference histogram can't, cause it only times the model call. `@ai-sdk/otel` bundles its own copy of `@opentelemetry/api`, so `agent.mjs` passes its tracer in directly instead of relying on the global one.

`agent.task.checks` is the "is it doing what it was asked" part. Every question says which tools a correct run has to call and which words the answer has to contain, and every finished run is checked three ways: `used_required_tools`, `answered` and `on_topic`. It's simple on purpose. A model that answers "checkout looks fine" without calling `get_error_rate` fails, and you see which model does that on which kind of question.

Every question also gets an `oncall_question` span above the AI SDK's `invoke_agent` span, with `agent.task`, `agent.outcome` and `agent.checks.failed` on it. The metrics say how many runs failed a check, the span says which run, so you can open it and see every model call and tool call it made. The Collector also adds `agent.task` and `agent.outcome` as `spanmetrics` dimensions, so run time and outcome per kind of question are in Prometheus too.

## The dashboard

Live, running on Grafana Cloud: https://brightparlor286.grafana.net/public-dashboards/5277cad1e14448b58d05284a54404e86

`dashboard/dashboard.json`, imports into any Grafana with a Prometheus and a Tempo data source (Dashboards > New > Import). Panels, with the detail rows folded so the answer comes first:

- right now: runs finished in the last 15 minutes (zero means it stopped), task checks passed, p95 model call, failed calls, and spend per month at this pace against the budget
- both models side by side for the last 6 hours: p95 model call and whole run, how often each called the tools it needed and named what was asked, how often the deadline cut it off, tokens and cost per 1,000 runs
- the react-inp-blame fix explainer: explanations in the last hour, how often it named the culprit and the right fix, p95 model call and p95 per explanation, and one row per interaction with the click as measured in Chrome
- p50 and p95 latency per model, and a heatmap of the histogram's own buckets
- tokens per minute and per call, split input and output
- cost per hour and per run at list price, spend against a daily budget, projected spend per day
- task checks per model and per kind of question
- runs by outcome, failed calls by `error.type`, tool calls
- from the spans: p95 per run, model call and tool, where a run's time goes, model calls per run
- model calls that failed for any reason other than the deadline, marked on every chart
- p95 run time and outcomes per kind of question, from the `oncall_question` span
- from Tempo: runs that failed a check, runs cut off by their deadline and runs over 8 seconds, counted with TraceQL metrics. Signed in, each bar links to its traces. Shared dashboards don't run Tempo queries, so the public copy leaves this row out

Cost is tokens times the list price in `dashboard/prices.json`. The calls here run on the free tier, so the cost panels show what the same traffic would cost on the paid tier. Gemma has no paid price on the Gemini API, so it shows as zero, or "no paid price" in the table. To change prices or the budget, edit `prices.json` and run `python3 dashboard/build.py`. `python3 dashboard/build.py <prometheus-uid> <tempo-uid>` writes a copy bound to two data sources, without the Tempo row, for a public dashboard.

## A second service on a real workload

The on-call agent answers made-up questions, so there's a second service on something real. [react-inp-blame](https://github.com/adityareddy-dev/react-inp-blame) is my library that says which React component made a click slow. `explainer/explainer.mjs` opens its live demo at [reactinpblame.com](https://reactinpblame.com) in a headless Chrome, makes one of the demo's slow interactions (an unstable context value, a layout thrash, a 3000-row list on each keystroke and so on), and reads the report the library writes for it. Then a model on the AI SDK, Gemini 3.1 Flash-Lite on the free tier, gets that report. It has one tool, `get_component_source`, which returns the real source of the demo's components from `explainer/source/`. It says why the interaction was slow and what to change.

Every answer gets three checks: `read_source` (it looked at the code), `named_culprit` (it names the component or handler the library blamed) and `right_fix` (it proposes the fix the demo's lab page lists for that interaction, like useMemo, virtualising or moving state down). The model never sees that page.

It's instrumented the same way as the agent: `gen_ai.client.inference.duration` from `@ai-sdk/otel`, `gen_ai.client.token.usage`, and an `explain_interaction` span above the AI SDK's spans, with a `measure_interaction` child for the time in Chrome. It also records `inp_explainer.interaction.duration`, how long each click took in the browser, so the dashboard puts the real slow click next to the time it took to explain it. The two services write the same metric names, so every panel filters on `service_name`.

The lab's cascading-effect interaction is left out. On the production build the click itself stays under the report threshold, and the heavy render comes after the paint.

## Run it

```sh
cp .env.example .env   # Grafana Cloud OTLP endpoint and token, a Gemini API key
docker compose up -d
```

The pause per model is in `MODELS`. The defaults keep Gemini 3.5 Flash-Lite under its free tier's 500 requests a day and Gemma 4 26B well under its 14,400. One run in ten gets a 1.5 second deadline on purpose, so timeouts show up in the failed calls panel. The explainer's model and pause are in `INP_MODEL`. One run every eight minutes on average keeps Gemini 3.1 Flash-Lite inside its free tier.

To use a local model instead, start Ollama and add `ollama/<model>@<pause>` to `MODELS`, with `OLLAMA_URL` pointing at its `/v1` endpoint.
