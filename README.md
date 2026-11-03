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

Token usage is recorded in the agent cause the AI SDK puts usage on spans, not on a metric.

`agent.task.checks` is the "is it doing what it was asked" part. Every question says which tools a correct run has to call and which words the answer has to contain, and every finished run is checked three ways: `used_required_tools`, `answered` and `on_topic`. It's simple on purpose. A model that answers "checkout looks fine" without calling `get_error_rate` fails, and you see which model does that on which kind of question.

## The dashboard

Live, running on Grafana Cloud: https://brightparlor286.grafana.net/public-dashboards/5277cad1e14448b58d05284a54404e86

`dashboard/dashboard.json`, imports into any Grafana with a Prometheus data source (Dashboards > New > Import). Panels:

- model calls, p95 latency, tokens, cost and failed calls for the last hour
- p50 and p95 latency per model, and a heatmap of the histogram's own buckets
- tokens per minute and per call, split input and output
- cost per hour and per run at list price, spend against a daily budget, projected spend per day
- task checks per model and per kind of question
- runs by outcome, failed calls by `error.type`, tool calls

Cost is tokens times the list price in `dashboard/prices.json`. The calls here run on the free tier, so the cost panels show what the same traffic would cost on the paid tier. Gemma has no paid price on the Gemini API, so it shows as zero. To change prices or the budget, edit `prices.json` and run `python3 dashboard/build.py`.

## Run it

```sh
cp .env.example .env   # Grafana Cloud OTLP endpoint and token, a Gemini API key
docker compose up -d
```

The pause per model is in `MODELS`. The defaults keep Gemini 3.5 Flash-Lite under its free tier's 500 requests a day and Gemma 4 26B well under its 14,400. One run in ten gets a 1.5 second deadline on purpose, so timeouts show up in the failed calls panel.

To use a local model instead, start Ollama and add `ollama/<model>@<pause>` to `MODELS`, with `OLLAMA_URL` pointing at its `/v1` endpoint.
