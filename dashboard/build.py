"""Writes the Grafana dashboard as JSON.

python3 build.py            -> portable file with data source pickers, for this repo
python3 build.py <prom-uid> <tempo-uid>  -> file bound to one Prometheus and one Tempo data source, for a live stack
"""
import json, os, sys

UID = sys.argv[1] if len(sys.argv) > 1 else None
DS = {'type': 'prometheus', 'uid': UID or '${datasource}'}
TDS = {'type': 'tempo', 'uid': sys.argv[2] if UID else '${tempo}'}
OUT = os.path.dirname(os.path.abspath(__file__))

# Dark-surface steps of the categorical order; slot 1 and 2 carry the two things
# each dashboard compares, red is kept for errors only.
BLUE, ORANGE, AQUA, YELLOW, MAGENTA, VIOLET = '#3987e5', '#d95926', '#199e70', '#c98500', '#d55181', '#9085e9'
RED, GOOD = '#e66767', '#199e70'


def target(expr, legend='', ref='A', **kw):
    t = {'datasource': DS, 'expr': expr, 'legendFormat': legend or '__auto', 'refId': ref, 'range': True}
    t.update(kw)
    return t


def color_override(name, color, regex=False):
    return {'matcher': {'id': 'byRegexp' if regex else 'byName', 'options': name},
            'properties': [{'id': 'color', 'value': {'mode': 'fixed', 'fixedColor': color}}]}


class Board:
    def __init__(self):
        self.panels, self.y, self.id = [], 0, 1

    def add(self, panel, w, h, x=0, newrow=False):
        panel['id'] = self.id
        self.id += 1
        panel['gridPos'] = {'x': x, 'y': self.y, 'w': w, 'h': h}
        self.panels.append(panel)
        if x + w >= 24:
            self.y += h

    def row(self, title):
        self.add({'type': 'row', 'title': title, 'collapsed': False, 'panels': []}, 24, 1)


def text(md):
    # Shared dashboards query every panel and fail one with no queries, so the
    # text panel carries a constant query it never shows.
    return {'type': 'text', 'title': '', 'options': {'mode': 'markdown', 'content': md},
            'datasource': DS, 'targets': [target('vector(1)', instant=True, range=False)], 'transparent': True}


def stat(title, expr, unit, desc='', decimals=None, color=BLUE, thresholds=None):
    p = {'type': 'stat', 'title': title, 'description': desc, 'datasource': DS,
         'targets': [target(expr, instant=True, range=False)],
         'fieldConfig': {'defaults': {'unit': unit, 'color': {'mode': 'fixed', 'fixedColor': color},
                                      'thresholds': thresholds or {'mode': 'absolute', 'steps': [{'color': color, 'value': None}]}},
                         'overrides': []},
         'options': {'reduceOptions': {'calcs': ['lastNotNull'], 'fields': '', 'values': False},
                     'colorMode': 'value', 'graphMode': 'none', 'textMode': 'value', 'justifyMode': 'center',
                     'wideLayout': True, 'showPercentChange': False}}
    if thresholds:
        p['fieldConfig']['defaults']['color'] = {'mode': 'thresholds'}
    if decimals is not None:
        p['fieldConfig']['defaults']['decimals'] = decimals
    return p


def series(title, targets, unit, desc='', overrides=(), stack=False, bars=False, min0=True, legend_calcs=('mean', 'max'), max_=None):
    custom = {'lineWidth': 2, 'fillOpacity': 0 if not (stack or bars) else 80, 'showPoints': 'never',
              'drawStyle': 'bars' if bars else 'line', 'spanNulls': True, 'gradientMode': 'none',
              'axisSoftMin': 0 if min0 else None, 'insertNulls': False,
              'stacking': {'mode': 'normal' if stack else 'none', 'group': 'A'}}
    return {'type': 'timeseries', 'title': title, 'description': desc, 'datasource': DS, 'targets': list(targets),
            'fieldConfig': {'defaults': {'unit': unit, 'custom': custom, 'color': {'mode': 'palette-classic'},
                                         **({'max': max_} if max_ is not None else {'max': 1, 'min': 0} if unit == 'percentunit' else {})},
                            'overrides': list(overrides)},
            'options': {'legend': {'displayMode': 'table', 'placement': 'bottom', 'calcs': list(legend_calcs), 'showLegend': True},
                        'tooltip': {'mode': 'multi', 'sort': 'desc'}}}


def heatmap(title, expr, desc=''):
    return {'type': 'heatmap', 'title': title, 'description': desc, 'datasource': DS,
            'targets': [target(expr, '{{le}}', format='heatmap')],
            'options': {'calculate': False, 'yAxis': {'unit': 's', 'axisPlacement': 'left'},
                        'cellGap': 2, 'color': {'mode': 'scheme', 'scheme': 'Blues', 'steps': 64, 'exponent': 0.5, 'fill': BLUE},
                        'rowsFrame': {'layout': 'auto'}, 'tooltip': {'mode': 'single', 'yHistogram': True},
                        'legend': {'show': True}, 'filterValues': {'le': 1e-9}},
            'fieldConfig': {'defaults': {}, 'overrides': []}}


def tempo_series(title, query, legend, desc='', overrides=()):
    # A TraceQL metrics query, counted from spans in Tempo. Search queries stream
    # over a websocket that shared dashboards don't have; metrics queries don't.
    p = series(title, [], 'short', desc, overrides=overrides, bars=True, stack=True, legend_calcs=('sum',))
    p['datasource'] = TDS
    p['targets'] = [{'datasource': TDS, 'refId': 'A', 'queryType': 'traceql', 'query': query,
                     'metricsQueryType': 'range', 'step': '5m', 'legendFormat': legend}]
    p['fieldConfig']['defaults']['custom']['barAlignment'] = -1
    return p


def dashboard(uid, title, desc, tags, panels, refresh='1m', time='now-6h'):
    d = {'uid': uid, 'title': title, 'description': desc, 'tags': tags, 'timezone': 'browser',
         'editable': True, 'graphTooltip': 1, 'refresh': refresh, 'schemaVersion': 41,
         'time': {'from': time, 'to': 'now'}, 'templating': {'list': []}, 'annotations': {'list': []},
         'links': [], 'panels': panels}
    if not UID:
        # Shared dashboards on a live stack can't use variables, so only the repo copy has the picker.
        d['templating']['list'].append({'name': 'datasource', 'label': 'Prometheus', 'type': 'datasource',
                                        'query': 'prometheus', 'current': {}, 'hide': 0, 'refresh': 1})
        if any(p.get('datasource') == TDS for p in panels):
            d['templating']['list'].append({'name': 'tempo', 'label': 'Tempo', 'type': 'datasource',
                                            'query': 'tempo', 'current': {}, 'hide': 0, 'refresh': 1})
    return d


# ---------------------------------------------------------------- AI agent
PRICES = json.load(open(os.path.join(OUT, 'prices.json')))
INF = 'gen_ai_client_inference_duration_seconds'
TOK = 'gen_ai_client_token_usage'


def cost_expr(window):
    # USD per model: tokens in the window times the list price per token.
    parts = []
    for model, p in PRICES['models'].items():
        for kind in ('input', 'output'):
            per_token = p[kind] / 1e6
            parts.append(f'sum by (gen_ai_request_model) (increase({TOK}_sum{{gen_ai_request_model="{model}", gen_ai_token_type="{kind}"}}[{window}])) * {per_token:.12g}')
    return 'sum by (gen_ai_request_model) (\n  ' + '\n  or\n  '.join(f'({x})' for x in parts) + '\n)'


def cost_total(window):
    return f'sum({cost_expr(window)})'


ai = Board()
ai.add(text(
    '### An AI agent, measured with the AI SDK\'s own OpenTelemetry metric\n'
    'A small on-call helper agent built on the [AI SDK](https://ai-sdk.dev) answers questions about a shop with two tools. '
    'It runs around the clock on two models through the Gemini API free tier: Gemini 3.5 Flash-Lite and the open-weights Gemma 4 26B. '
    'Latency comes from `gen_ai.client.inference.duration`, the histogram [@ai-sdk/otel](https://www.npmjs.com/package/@ai-sdk/otel) records '
    'for every model call. I asked for it in [vercel/ai#24229](https://github.com/vercel/ai/issues/24229) and it shipped in @ai-sdk/otel 1.0.160. '
    'Tokens come from the semconv `gen_ai.client.token.usage` histogram. Cost is tokens times each model\'s list price, '
    f'so it shows what this traffic would cost on the paid tier. Gemma has no paid price, so its cost is zero. Prices as of {PRICES["as_of"]}.'), 24, 4)
ai.row('Last hour')
ai.add(stat('Model calls', f'sum(increase({INF}_count[1h]))', 'short', 'Calls to a model in the last hour, every outcome.', 0), 4, 4, 0)
ai.add(stat('p95 inference latency', f'histogram_quantile(0.95, sum by (le) (rate({INF}_bucket[1h])))', 's', '95th percentile of gen_ai.client.inference.duration over the last hour, all models.', 2, ORANGE), 5, 4, 4)
ai.add(stat('Tokens', f'sum(increase({TOK}_sum[1h]))', 'short', 'Input plus output tokens in the last hour.', 0, AQUA), 5, 4, 9)
ai.add(stat('Cost at list price', cost_total('1h'), 'currencyUSD', 'Last hour of tokens times list price per model.', 4, YELLOW), 5, 4, 14)
ai.add(stat('Calls that failed', f'sum(increase({INF}_count{{error_type!=""}}[1h])) / sum(increase({INF}_count[1h]))', 'percentunit',
            'Share of model calls the histogram recorded with error.type set (timeouts, rate limits, server errors).', 1,
            thresholds={'mode': 'absolute', 'steps': [{'color': GOOD, 'value': None}, {'color': YELLOW, 'value': 0.05}, {'color': RED, 'value': 0.15}]}), 5, 4, 19)

ai.row('Latency: gen_ai.client.inference.duration')
model_colors = [color_override('.*gemma.*', BLUE, True), color_override('.*gemini.*', ORANGE, True)]
ai.add(series('p50 and p95 inference latency by model', [
    target(f'histogram_quantile(0.50, sum by (le, gen_ai_request_model) (rate({INF}_bucket{{error_type=""}}[15m])))', 'p50 {{gen_ai_request_model}}', 'A'),
    target(f'histogram_quantile(0.95, sum by (le, gen_ai_request_model) (rate({INF}_bucket{{error_type=""}}[15m])))', 'p95 {{gen_ai_request_model}}', 'B')],
    's', 'Successful calls only, rolling 15 minutes. The p95 line is dashed.',
    overrides=model_colors + [{'matcher': {'id': 'byRegexp', 'options': '^p95.*'},
                               'properties': [{'id': 'custom.lineStyle', 'value': {'fill': 'dash', 'dash': [10, 6]}}]}]), 14, 11, 0)
ai.add(heatmap('Where model calls land, by bucket', f'sum by (le) (increase({INF}_bucket[$__rate_interval]))',
               'Every call, both models, in the histogram\'s own bucket boundaries (0.01 s to 81.92 s).'), 10, 11, 14)

ai.row('Tokens and cost')
ai.add(series('Tokens per minute, input and output', [
    target(f'sum by (gen_ai_request_model, gen_ai_token_type) (rate({TOK}_sum[$__rate_interval])) * 60', '{{gen_ai_request_model}} {{gen_ai_token_type}}')],
    'short', 'From gen_ai.client.token.usage, recorded once per model call.', stack=True,
    overrides=[color_override('.*gemma.*input', BLUE, True), color_override('.*gemma.*output', VIOLET, True),
               color_override('.*gemini.*input', ORANGE, True), color_override('.*gemini.*output', YELLOW, True)]), 12, 10, 0)
ai.add(series('Cost per hour at list price', [target(cost_expr('1h'), '{{gen_ai_request_model}}')],
              'currencyUSD', 'Rolling one-hour token cost per model. List prices in prices.json next to this dashboard.',
              overrides=model_colors), 12, 10, 12)
ai.add(series('Tokens per model call (average)', [
    target(f'sum by (gen_ai_request_model, gen_ai_token_type) (rate({TOK}_sum[$__rate_interval])) / sum by (gen_ai_request_model, gen_ai_token_type) (rate({TOK}_count[$__rate_interval]))', '{{gen_ai_request_model}} {{gen_ai_token_type}}')],
    'short', 'Tool results make input grow with each step of a run.',
    overrides=[color_override('.*gemma.*input', BLUE, True), color_override('.*gemma.*output', VIOLET, True),
               color_override('.*gemini.*input', ORANGE, True), color_override('.*gemini.*output', YELLOW, True)]), 12, 10, 0)
ai.add(series('Cost per agent run', [
    target(f'{cost_expr("1h")} / sum by (gen_ai_request_model) (increase(agent_runs_total[1h]))', '{{gen_ai_request_model}}')],
    'currencyUSD', 'One-hour cost divided by runs finished in that hour.', overrides=model_colors), 12, 10, 12)

ai.row('Doing what it was asked, within budget')
CHK = 'agent_task_checks_total'
ai.add(series('Runs that passed each check', [
    target(f'sum by (check) (increase({CHK}{{result="pass"}}[1h])) / sum by (check) (increase({CHK}[1h]))', '{{check}}')],
    'percentunit', 'Rolling one hour. used_required_tools: the run called every tool its question needs. answered: the reply is not empty. on_topic: the reply names what was asked about.',
    overrides=[color_override('used_required_tools', BLUE), color_override('answered', AQUA), color_override('on_topic', VIOLET)]), 9, 10, 0)
ai.add(series('Required tools used, by model and task', [
    target(f'sum by (gen_ai_request_model, agent_task) (increase({CHK}{{check="used_required_tools", result="pass"}}[6h])) / sum by (gen_ai_request_model, agent_task) (increase({CHK}{{check="used_required_tools"}}[6h]))', '{{gen_ai_request_model}} {{agent_task}}')],
    'percentunit', 'Rolling six hours. Shows which model skips a tool on which kind of question.', legend_calcs=('lastNotNull',)), 9, 10, 9)
BUDGET = PRICES['daily_budget_usd']
b = stat('Spend in the last 24 hours, share of daily budget', f'sum({cost_expr("24h")}) / {BUDGET}', 'percentunit',
         f'Last 24 hours of tokens at list price against a ${BUDGET:.2f} daily budget.', 1,
         thresholds={'mode': 'absolute', 'steps': [{'color': GOOD, 'value': None}, {'color': YELLOW, 'value': 0.8}, {'color': RED, 'value': 1}]})
b['options']['graphMode'] = 'none'
ai.add(b, 6, 5, 18)
ai.add(stat('Projected spend per day', f'sum({cost_expr("1h")}) * 24', 'currencyUSD', 'Last hour of cost times 24.', 2, YELLOW), 6, 5, 18)

ai.row('The agent loop')
ai.add(series('Agent runs by outcome', [
    target('sum by (outcome) (increase(agent_runs_total[$__rate_interval]))', '{{outcome}}')],
    'short', 'One run is a question answered with up to four model calls. One run in ten gets a 1.5 s deadline on purpose.',
    bars=True, stack=True, legend_calcs=('sum',),
    overrides=[color_override('ok', GOOD), color_override('timeout', YELLOW), color_override('error', RED)]), 8, 9, 0)
ai.add(series('Failed model calls by error.type', [
    target(f'sum by (error_type, gen_ai_request_model) (increase({INF}_count{{error_type!=""}}[$__rate_interval])) > 0', '{{gen_ai_request_model}} {{error_type}}')],
    'short', 'The inference histogram records failed calls too, with error.type set.', bars=True, stack=True, legend_calcs=('sum',),
    overrides=model_colors), 8, 9, 8)
ai.add(series('Tool calls the model asked for', [
    target('sum by (gen_ai_tool_name) (increase(agent_tool_calls_total[$__rate_interval]))', '{{gen_ai_tool_name}}')],
    'short', 'get_error_rate and get_runbook.', bars=True, stack=True, legend_calcs=('sum',),
    overrides=[color_override('get_error_rate', AQUA), color_override('get_runbook', MAGENTA)]), 8, 9, 16)

SM = 'traces_span_metrics_duration_seconds'
ai.row('Spans from @ai-sdk/otel, turned into metrics by the Collector')
ai.add(series('p95 duration from spans: runs, model calls, tools', [
    target(f'histogram_quantile(0.95, sum by (le, gen_ai_request_model) (rate({SM}_bucket{{gen_ai_operation_name="invoke_agent"}}[15m])))', 'run {{gen_ai_request_model}}', 'A'),
    target(f'histogram_quantile(0.95, sum by (le, gen_ai_request_model) (rate({SM}_bucket{{gen_ai_operation_name="chat"}}[15m])))', 'model call {{gen_ai_request_model}}', 'B'),
    target(f'histogram_quantile(0.95, sum by (le, gen_ai_tool_name) (rate({SM}_bucket{{gen_ai_operation_name="execute_tool"}}[15m])))', 'tool {{gen_ai_tool_name}}', 'C')],
    's', 'The Collector\'s spanmetrics connector reads the invoke_agent, chat and execute_tool spans the AI SDK emits and writes duration histograms. Rolling 15 minutes. Dashed lines are model calls.',
    overrides=[color_override('.*gemma.*', BLUE, True), color_override('.*gemini.*', ORANGE, True),
               color_override('tool get_error_rate', AQUA), color_override('tool get_runbook', MAGENTA),
               {'matcher': {'id': 'byRegexp', 'options': '^model call.*'},
                'properties': [{'id': 'custom.lineStyle', 'value': {'fill': 'dash', 'dash': [10, 6]}}]}]), 12, 11, 0)
ai.add(series('Where a run\'s time goes', [
    target(f'sum by (gen_ai_request_model) (rate({SM}_sum{{gen_ai_operation_name="chat"}}[15m])) / sum by (gen_ai_request_model) (rate({SM}_count{{gen_ai_operation_name="invoke_agent"}}[15m]))', 'in model calls, {{gen_ai_request_model}}', 'A'),
    target(f'sum(rate({SM}_sum{{gen_ai_operation_name="execute_tool"}}[15m])) / sum(rate({SM}_count{{gen_ai_operation_name="invoke_agent"}}[15m]))', 'in tools, all models', 'B')],
    's', 'Average seconds per run, from span durations. Model time is nearly all of it.',
    overrides=[color_override('.*gemma.*', BLUE, True), color_override('.*gemini.*', ORANGE, True), color_override('in tools, all models', AQUA)]), 6, 11, 12)
ai.add(series('Model calls per run', [
    target(f'sum by (gen_ai_request_model) (rate({SM}_count{{gen_ai_operation_name="chat"}}[30m])) / sum by (gen_ai_request_model) (rate({SM}_count{{gen_ai_operation_name="invoke_agent"}}[30m]))', '{{gen_ai_request_model}}')],
    'short', 'chat spans per invoke_agent span, rolling 30 minutes. Each tool round trip adds a call.', overrides=model_colors), 6, 11, 18)
Q = 'span_name="oncall_question", agent_task!=""'
ai.add(series('p95 run time by kind of question', [
    target(f'histogram_quantile(0.95, sum by (le, agent_task) (rate({SM}_bucket{{{Q}}}[30m])))', '{{agent_task}}')],
    's', 'From the agent\'s oncall_question span, which wraps a whole run. Rolling 30 minutes. Questions that need a tool and a runbook take more round trips.'), 12, 9, 0)
ai.add(series('Runs by kind of question and outcome', [
    target(f'sum by (agent_task, agent_outcome) (increase(traces_span_metrics_calls_total{{{Q}}}[$__rate_interval])) > 0', '{{agent_task}} {{agent_outcome}}')],
    'short', 'Counted from the oncall_question spans. Shows which kinds of question hit the 1.5 second deadline.',
    bars=True, stack=True, legend_calcs=('sum',)), 12, 9, 12)

if not UID:
    # Shared dashboards don't run Tempo queries, so only the repo copy has this row.
    ai.row('Which runs, from the traces in Tempo')
    ai.add(tempo_series('Runs that failed a check, by check and model',
                        '{ name = "oncall_question" && span.agent.checks.failed != nil } | count_over_time() by (span.agent.checks.failed, span.gen_ai.request.model)',
                        '', 'Counted from the oncall_question spans in 5 minute steps. agent.checks.failed lists every check a run failed, so a run that skipped its tool and also left out the service shows as both.'), 8, 9, 0)
    ai.add(tempo_series('Runs cut off by their deadline, by kind of question',
                        '{ name = "oncall_question" && span.agent.outcome = "timeout" } | count_over_time() by (span.agent.task)',
                        '', 'One run in ten gets a 1.5 second deadline on purpose. Runs with more tool round trips get cut off more.'), 8, 9, 8)
    ai.add(tempo_series('Runs over 8 seconds, by kind of question and model',
                        '{ name = "oncall_question" && duration > 8s } | count_over_time() by (span.agent.task, span.gen_ai.request.model)',
                        '', 'Slow runs, counted from spans. Signed in, each bar links to the traces behind it.'), 8, 9, 16)

ai_json = dashboard('ai-agent-genai-otel', 'AI agent: latency, tokens and cost from @ai-sdk/otel',
                    'gen_ai.client.inference.duration from @ai-sdk/otel, plus token usage and list-price cost, for an AI SDK agent on two models.',
                    ['ai-sdk', 'opentelemetry', 'gen-ai', 'golden-grot'], ai.panels)

suffix = '' if not UID else '.live'
with open(os.path.join(OUT, f'dashboard{suffix}.json'), 'w') as f:
    json.dump(ai_json, f, indent=2)
    f.write('\n')
print('wrote', suffix or 'portable', 'dashboard')
