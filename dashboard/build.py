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


def collapse(panels, titles):
    # Folds the named rows: their panels move inside the row and open on click.
    out, row = [], None
    for p in panels:
        if p['type'] == 'row':
            row = p if p['title'] in titles else None
            if row:
                row['collapsed'] = True
            out.append(p)
        elif row:
            row['panels'].append(p)
        else:
            out.append(p)
    return out


def compare(title, columns, desc='', key='gen_ai_request_model', key_name='Model'):
    # One row per model (or other key label), one column per instant query, joined on that label.
    targets = [target(expr, '', chr(65 + i), instant=True, range=False, format='table') for i, (_, expr, _, _) in enumerate(columns)]
    rename = {f'Value #{chr(65 + i)}': name for i, (name, _, _, _) in enumerate(columns)}
    rename[key] = key_name
    overrides = []
    for name, _, unit, extra in columns:
        props = [{'id': 'unit', 'value': unit}] + list(extra)
        overrides.append({'matcher': {'id': 'byName', 'options': name}, 'properties': props})
    return {'type': 'table', 'title': title, 'description': desc, 'datasource': DS, 'targets': targets,
            'transformations': [{'id': 'merge', 'options': {}},
                                {'id': 'organize', 'options': {'excludeByName': {'Time': True}, 'renameByName': rename,
                                                               'indexByName': {key: 0, **{f'Value #{chr(65 + i)}': i + 1 for i in range(len(columns))}}}}],
            'fieldConfig': {'defaults': {'custom': {'align': 'auto', 'cellOptions': {'type': 'auto'}}}, 'overrides': overrides},
            'options': {'showHeader': True, 'cellHeight': 'md', 'footer': {'show': False}}}


def tempo_series(title, query, legend, desc='', overrides=()):
    # A TraceQL metrics query, counted from spans in Tempo. Search queries stream
    # over a websocket that shared dashboards don't have; metrics queries don't.
    p = series(title, [], 'short', desc, overrides=overrides, bars=True, stack=True, legend_calcs=('sum',))
    p['datasource'] = TDS
    p['targets'] = [{'datasource': TDS, 'refId': 'A', 'queryType': 'traceql', 'query': query,
                     'metricsQueryType': 'range', 'step': '5m', 'legendFormat': legend}]
    p['fieldConfig']['defaults']['custom']['barAlignment'] = -1
    return p


def dashboard(uid, title, desc, tags, panels, refresh='1m', time='now-6h', annotations=()):
    d = {'uid': uid, 'title': title, 'description': desc, 'tags': tags, 'timezone': 'browser',
         'editable': True, 'graphTooltip': 1, 'refresh': refresh, 'schemaVersion': 41,
         'time': {'from': time, 'to': 'now'}, 'templating': {'list': []}, 'annotations': {'list': list(annotations)},
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
# The two AI services write the same metric names, so each panel picks its service.
OA = 'service_name="oncall-agent"'
IX = 'service_name="react-inp-blame-explainer"'


def cost_expr(window):
    # USD per model: tokens in the window times the list price per token.
    parts = []
    for model, p in PRICES['models'].items():
        for kind in ('input', 'output'):
            per_token = p[kind] / 1e6
            parts.append(f'sum by (gen_ai_request_model) (increase({TOK}_sum{{{OA}, gen_ai_request_model="{model}", gen_ai_token_type="{kind}"}}[{window}])) * {per_token:.12g}')
    return 'sum by (gen_ai_request_model) (\n  ' + '\n  or\n  '.join(f'({x})' for x in parts) + '\n)'


def cost_total(window):
    return f'sum({cost_expr(window)})'


ai = Board()
ai.add(text(
    '### Two AI SDK services, measured with the AI SDK\'s own OpenTelemetry metric\n'
    'A small on-call helper agent built on the [AI SDK](https://ai-sdk.dev) answers questions about a shop with two tools. '
    'It runs around the clock on two models through the Gemini API free tier: Gemini 3.5 Flash-Lite and the open-weights Gemma 4 26B. '
    'Latency comes from `gen_ai.client.inference.duration`, the histogram [@ai-sdk/otel](https://www.npmjs.com/package/@ai-sdk/otel) records '
    'for every model call. I asked for it in [vercel/ai#24229](https://github.com/vercel/ai/issues/24229) and it shipped in @ai-sdk/otel 1.0.160. '
    'Tokens come from the semconv `gen_ai.client.token.usage` histogram. Cost is tokens times each model\'s list price, '
    f'so it shows what this traffic would cost on the paid tier. Gemma has no paid price, so its cost is zero. Prices as of {PRICES["as_of"]}. '
    'Further down, the second service runs a real workload: a fix explainer for my react-inp-blame library, on its live demo.'), 24, 4)
BUDGET = PRICES['daily_budget_usd']
CHK = 'agent_task_checks_total'
SM = 'traces_span_metrics_duration_seconds'
ai.row('The on-call agent, right now')
ai.add(stat('Runs finished, last 15 minutes', 'sum(increase(agent_runs_total[15m]))', 'short',
            'Zero means the agent has stopped. Every model runs its own loop, so a stuck model shows up here.', 0,
            thresholds={'mode': 'absolute', 'steps': [{'color': RED, 'value': None}, {'color': GOOD, 'value': 1}]}), 5, 4, 0)
ai.add(stat('Task checks passed, last hour', f'sum(increase({CHK}{{result="pass"}}[1h])) / sum(increase({CHK}[1h]))', 'percentunit',
            'Did it do what it was asked: called the tools the question needs, answered, and named what was asked about. All three checks on every finished run.', 1,
            thresholds={'mode': 'absolute', 'steps': [{'color': RED, 'value': None}, {'color': YELLOW, 'value': 0.8}, {'color': GOOD, 'value': 0.95}]}), 5, 4, 5)
ai.add(stat('p95 model call, last hour', f'histogram_quantile(0.95, sum by (le) (rate({INF}_bucket{{{OA}}}[1h])))', 's',
            '95th percentile of gen_ai.client.inference.duration over the last hour, all models.', 2, ORANGE), 5, 4, 10)
ai.add(stat('Calls that failed, last hour', f'sum(increase({INF}_count{{{OA}, error_type!=""}}[1h])) / sum(increase({INF}_count{{{OA}}}[1h]))', 'percentunit',
            'Share of model calls the histogram recorded with error.type set. Most are the 1.5 second deadline one run in ten gets on purpose.', 1,
            thresholds={'mode': 'absolute', 'steps': [{'color': GOOD, 'value': None}, {'color': YELLOW, 'value': 0.08}, {'color': RED, 'value': 0.15}]}), 4, 4, 15)
ai.add(stat('Spend per month at this pace', f'sum({cost_expr("1h")}) * 24 * 30', 'currencyUSD',
            f'Last hour of tokens at list price, times 24 times 30. Budget ${BUDGET * 30:.0f} a month. Gemma 4 26B has no paid price on the Gemini API, so it adds nothing.', 2,
            thresholds={'mode': 'absolute', 'steps': [{'color': GOOD, 'value': None}, {'color': YELLOW, 'value': BUDGET * 30 * 0.8}, {'color': RED, 'value': BUDGET * 30}]}), 5, 4, 19)

ai.row('Which model does the job')
NO_PRICE = [{'id': 'mappings', 'value': [{'type': 'value', 'options': {'0': {'text': 'no paid price'}}}]}]
ai.add(compare('Both models, last 6 hours', [
    ('p95 model call', f'histogram_quantile(0.95, sum by (le, gen_ai_request_model) (rate({INF}_bucket{{{OA}, error_type=""}}[6h])))', 's', [{'id': 'decimals', 'value': 2}]),
    ('p95 whole run', f'histogram_quantile(0.95, sum by (le, gen_ai_request_model) (rate({SM}_bucket{{{OA}, gen_ai_operation_name="invoke_agent"}}[6h])))', 's', [{'id': 'decimals', 'value': 1}]),
    ('Called the tools it needed', f'sum by (gen_ai_request_model) (increase({CHK}{{check="used_required_tools", result="pass"}}[6h])) / sum by (gen_ai_request_model) (increase({CHK}{{check="used_required_tools"}}[6h]))', 'percentunit', [{'id': 'decimals', 'value': 1}]),
    ('Named what was asked', f'sum by (gen_ai_request_model) (increase({CHK}{{check="on_topic", result="pass"}}[6h])) / sum by (gen_ai_request_model) (increase({CHK}{{check="on_topic"}}[6h]))', 'percentunit', [{'id': 'decimals', 'value': 1}]),
    ('Cut off by deadline', f'sum by (gen_ai_request_model) (increase(agent_runs_total{{outcome="timeout"}}[6h])) / sum by (gen_ai_request_model) (increase(agent_runs_total[6h]))', 'percentunit', [{'id': 'decimals', 'value': 1}]),
    ('Tokens per run', f'sum by (gen_ai_request_model) (increase({TOK}_sum{{{OA}}}[6h])) / sum by (gen_ai_request_model) (increase(agent_runs_total[6h]))', 'short', [{'id': 'decimals', 'value': 0}]),
    ('Cost per 1,000 runs', f'(({cost_expr("6h")}) or (sum by (gen_ai_request_model) (increase(agent_runs_total[6h])) * 0)) / sum by (gen_ai_request_model) (increase(agent_runs_total[6h])) * 1000', 'currencyUSD', [{'id': 'decimals', 'value': 3}] + NO_PRICE),
], 'The same questions go to both models all day. Read across a row to pick a model for this job: speed, whether it does what it was asked, and what it costs at list price.'), 24, 5)

ai.row('A real workload: the fix explainer for react-inp-blame')
ai.add(text(
    '[react-inp-blame](https://github.com/adityareddy-dev/react-inp-blame) is my library that says which React component made a click slow. '
    'Here a headless Chrome opens its live demo at [reactinpblame.com](https://reactinpblame.com), makes one of the demo\'s slow interactions, '
    'and reads the report the library writes. A model on the AI SDK (Gemini 3.1 Flash-Lite, Gemini API free tier) gets that report, reads the component\'s source with one tool, '
    'and says why the interaction was slow and what to change. Every answer is checked against the library\'s own blame and against the fix the demo documents, which the model never sees. '
    'Same instrumentation as the agent above: `gen_ai.client.inference.duration` from @ai-sdk/otel, token usage, and spans through the Collector.'), 24, 3)
IXR = 'inp_explainer_runs_total'
IXC = 'inp_explainer_checks_total'
IXD = 'inp_explainer_interaction_duration_seconds'
pct = {'mode': 'absolute', 'steps': [{'color': RED, 'value': None}, {'color': YELLOW, 'value': 0.7}, {'color': GOOD, 'value': 0.9}]}
ai.add(stat('Explanations, last hour', f'sum(increase({IXR}{{outcome="ok"}}[1h]))', 'short',
            'Interactions measured on the live demo and explained. Zero means it stopped.', 0,
            thresholds={'mode': 'absolute', 'steps': [{'color': RED, 'value': None}, {'color': GOOD, 'value': 1}]}), 5, 4, 0)
ai.add(stat('Named the component the library blamed, last 6 hours', f'sum(increase({IXC}{{check="named_culprit", result="pass"}}[6h])) / sum(increase({IXC}{{check="named_culprit"}}[6h]))', 'percentunit',
            'The answer names the component, handler or child react-inp-blame put the time on.', 1, thresholds=pct), 5, 4, 5)
ai.add(stat('Gave the fix the demo documents, last 6 hours', f'sum(increase({IXC}{{check="right_fix", result="pass"}}[6h])) / sum(increase({IXC}{{check="right_fix"}}[6h]))', 'percentunit',
            'The answer proposes the fix the demo\'s lab page lists for that interaction (useMemo, virtualise, move state down, a worker, and so on). The model never sees that page.', 1, thresholds=pct), 5, 4, 10)
ai.add(stat('p95 model call, last hour', f'histogram_quantile(0.95, sum by (le) (rate({INF}_bucket{{{IX}}}[1h])))', 's',
            'gen_ai.client.inference.duration from @ai-sdk/otel, for the explainer only.', 2, BLUE), 4, 4, 15)
ai.add(stat('p95 to explain one interaction, last hour', f'histogram_quantile(0.95, sum by (le) (rate({SM}_bucket{{{IX}, span_name="explain_interaction", agent_outcome="ok"}}[1h])))', 's',
            'From the explain_interaction span: measuring the click in Chrome, reading the source and writing the answer.', 1, BLUE), 5, 4, 19)
ixt = compare('Each slow interaction on the live demo, last 6 hours', [
    ('Measured in Chrome (median)', f'histogram_quantile(0.5, sum by (le, agent_task) (rate({IXD}_bucket[6h])))', 's', [{'id': 'decimals', 'value': 2}]),
    ('p95 to explain', f'histogram_quantile(0.95, sum by (le, agent_task) (rate({SM}_bucket{{{IX}, span_name="explain_interaction", agent_outcome="ok"}}[6h])))', 's', [{'id': 'decimals', 'value': 1}]),
    ('Read the source', f'sum by (agent_task) (increase({IXC}{{check="read_source", result="pass"}}[6h])) / sum by (agent_task) (increase({IXC}{{check="read_source"}}[6h]))', 'percentunit', [{'id': 'decimals', 'value': 0}]),
    ('Named the culprit', f'sum by (agent_task) (increase({IXC}{{check="named_culprit", result="pass"}}[6h])) / sum by (agent_task) (increase({IXC}{{check="named_culprit"}}[6h]))', 'percentunit', [{'id': 'decimals', 'value': 0}]),
    ('Right fix', f'sum by (agent_task) (increase({IXC}{{check="right_fix", result="pass"}}[6h])) / sum by (agent_task) (increase({IXC}{{check="right_fix"}}[6h]))', 'percentunit', [{'id': 'decimals', 'value': 0}]),
    ('Tokens per explanation', f'sum by (agent_task) (increase({TOK}_sum{{{IX}}}[6h])) / sum by (agent_task) (increase({IXR}{{outcome="ok"}}[6h]))', 'short', [{'id': 'decimals', 'value': 0}]),
], 'One row per interaction in the demo\'s anti-pattern lab. Read across: how slow the click really was, how long the explanation took, and whether it got the cause and the fix right.',
    key='agent_task', key_name='Interaction')
ixt['fieldConfig']['defaults']['mappings'] = [{'type': 'special', 'options': {'match': 'nan', 'result': {'text': '–'}}},
                                               {'type': 'special', 'options': {'match': 'null', 'result': {'text': '–'}}}]
ai.add(ixt, 24, 7)

ai.row('Latency: gen_ai.client.inference.duration')
model_colors = [color_override('.*gemma.*', BLUE, True), color_override('.*gemini.*', ORANGE, True)]
ai.add(series('p50 and p95 inference latency by model', [
    target(f'histogram_quantile(0.50, sum by (le, gen_ai_request_model) (rate({INF}_bucket{{{OA}, error_type=""}}[15m])))', 'p50 {{gen_ai_request_model}}', 'A'),
    target(f'histogram_quantile(0.95, sum by (le, gen_ai_request_model) (rate({INF}_bucket{{{OA}, error_type=""}}[15m])))', 'p95 {{gen_ai_request_model}}', 'B')],
    's', 'Successful calls only, rolling 15 minutes. The p95 line is dashed.',
    overrides=model_colors + [{'matcher': {'id': 'byRegexp', 'options': '^p95.*'},
                               'properties': [{'id': 'custom.lineStyle', 'value': {'fill': 'dash', 'dash': [10, 6]}}]}]), 14, 11, 0)
ai.add(heatmap('Where model calls land, by bucket', f'sum by (le) (increase({INF}_bucket{{{OA}}}[$__rate_interval]))',
               'Every call, both models, in the histogram\'s own bucket boundaries (0.01 s to 81.92 s).'), 10, 11, 14)

ai.row('Tokens and cost')
ai.add(series('Tokens per minute, input and output', [
    target(f'sum by (gen_ai_request_model, gen_ai_token_type) (rate({TOK}_sum{{{OA}}}[$__rate_interval])) * 60', '{{gen_ai_request_model}} {{gen_ai_token_type}}')],
    'short', 'From gen_ai.client.token.usage, recorded once per model call.', stack=True,
    overrides=[color_override('.*gemma.*input', BLUE, True), color_override('.*gemma.*output', VIOLET, True),
               color_override('.*gemini.*input', ORANGE, True), color_override('.*gemini.*output', YELLOW, True)]), 12, 10, 0)
ai.add(series('Cost per hour at list price', [target(cost_expr('1h'), '{{gen_ai_request_model}}')],
              'currencyUSD', 'Rolling one-hour token cost per model. List prices in prices.json next to this dashboard.',
              overrides=model_colors), 12, 10, 12)
ai.add(series('Tokens per model call (average)', [
    target(f'sum by (gen_ai_request_model, gen_ai_token_type) (rate({TOK}_sum{{{OA}}}[$__rate_interval])) / sum by (gen_ai_request_model, gen_ai_token_type) (rate({TOK}_count{{{OA}}}[$__rate_interval]))', '{{gen_ai_request_model}} {{gen_ai_token_type}}')],
    'short', 'Tool results make input grow with each step of a run.',
    overrides=[color_override('.*gemma.*input', BLUE, True), color_override('.*gemma.*output', VIOLET, True),
               color_override('.*gemini.*input', ORANGE, True), color_override('.*gemini.*output', YELLOW, True)]), 12, 10, 0)
ai.add(series('Cost per agent run', [
    target(f'{cost_expr("1h")} / sum by (gen_ai_request_model) (increase(agent_runs_total[1h]))', '{{gen_ai_request_model}}')],
    'currencyUSD', 'One-hour cost divided by runs finished in that hour.', overrides=model_colors), 12, 10, 12)

ai.row('Doing what it was asked, within budget')
ai.add(series('Runs that passed each check', [
    target(f'sum by (check) (increase({CHK}{{result="pass"}}[1h])) / sum by (check) (increase({CHK}[1h]))', '{{check}}')],
    'percentunit', 'Rolling one hour. used_required_tools: the run called every tool its question needs. answered: the reply is not empty. on_topic: the reply names what was asked about.',
    overrides=[color_override('used_required_tools', BLUE), color_override('answered', AQUA), color_override('on_topic', VIOLET)]), 9, 10, 0)
ai.add(series('Required tools used, by model and task', [
    target(f'sum by (gen_ai_request_model, agent_task) (increase({CHK}{{check="used_required_tools", result="pass"}}[6h])) / sum by (gen_ai_request_model, agent_task) (increase({CHK}{{check="used_required_tools"}}[6h]))', '{{gen_ai_request_model}} {{agent_task}}')],
    'percentunit', 'Rolling six hours. Shows which model skips a tool on which kind of question.', legend_calcs=('lastNotNull',)), 9, 10, 9)
b = stat('Spend in the last 24 hours, share of daily budget', f'sum({cost_expr("24h")}) / {BUDGET}', 'percentunit',
         f'Last 24 hours of tokens at list price against a ${BUDGET:.2f} daily budget.', 1,
         thresholds={'mode': 'absolute', 'steps': [{'color': GOOD, 'value': None}, {'color': YELLOW, 'value': 0.8}, {'color': RED, 'value': 1}]})
b['options']['graphMode'] = 'none'
ai.add(b, 6, 5, 18)
ai.add(stat('Cost per 1,000 runs, last 6 hours', f'sum({cost_expr("6h")}) / sum(increase(agent_runs_total[6h])) * 1000', 'currencyUSD',
            'Both models together at list price. Gemma 4 26B runs count, its cost is zero.', 3, YELLOW), 6, 5, 18)

ai.row('The agent loop')
ai.add(series('Agent runs by outcome', [
    target('sum by (outcome) (increase(agent_runs_total[$__rate_interval]))', '{{outcome}}')],
    'short', 'One run is a question answered with up to four model calls. One run in ten gets a 1.5 s deadline on purpose.',
    bars=True, stack=True, legend_calcs=('sum',),
    overrides=[color_override('ok', GOOD), color_override('timeout', YELLOW), color_override('error', RED)]), 8, 9, 0)
ai.add(series('Failed model calls by error.type', [
    target(f'sum by (error_type, gen_ai_request_model) (increase({INF}_count{{{OA}, error_type!=""}}[$__rate_interval])) > 0', '{{gen_ai_request_model}} {{error_type}}')],
    'short', 'The inference histogram records failed calls too, with error.type set.', bars=True, stack=True, legend_calcs=('sum',),
    overrides=model_colors), 8, 9, 8)
ai.add(series('Tool calls the model asked for', [
    target('sum by (gen_ai_tool_name) (increase(agent_tool_calls_total[$__rate_interval]))', '{{gen_ai_tool_name}}')],
    'short', 'get_error_rate and get_runbook.', bars=True, stack=True, legend_calcs=('sum',),
    overrides=[color_override('get_error_rate', AQUA), color_override('get_runbook', MAGENTA)]), 8, 9, 16)

ai.row('Spans from @ai-sdk/otel, turned into metrics by the Collector')
ai.add(series('p95 duration from spans: runs, model calls, tools', [
    target(f'histogram_quantile(0.95, sum by (le, gen_ai_request_model) (rate({SM}_bucket{{{OA}, gen_ai_operation_name="invoke_agent"}}[15m])))', 'run {{gen_ai_request_model}}', 'A'),
    target(f'histogram_quantile(0.95, sum by (le, gen_ai_request_model) (rate({SM}_bucket{{{OA}, gen_ai_operation_name="chat"}}[15m])))', 'model call {{gen_ai_request_model}}', 'B'),
    target(f'histogram_quantile(0.95, sum by (le, gen_ai_tool_name) (rate({SM}_bucket{{{OA}, gen_ai_operation_name="execute_tool"}}[15m])))', 'tool {{gen_ai_tool_name}}', 'C')],
    's', 'The Collector\'s spanmetrics connector reads the invoke_agent, chat and execute_tool spans the AI SDK emits and writes duration histograms. Rolling 15 minutes. Dashed lines are model calls.',
    overrides=[color_override('.*gemma.*', BLUE, True), color_override('.*gemini.*', ORANGE, True),
               color_override('tool get_error_rate', AQUA), color_override('tool get_runbook', MAGENTA),
               {'matcher': {'id': 'byRegexp', 'options': '^model call.*'},
                'properties': [{'id': 'custom.lineStyle', 'value': {'fill': 'dash', 'dash': [10, 6]}}]}]), 12, 11, 0)
ai.add(series('Where a run\'s time goes', [
    target(f'sum by (gen_ai_request_model) (rate({SM}_sum{{{OA}, gen_ai_operation_name="chat"}}[15m])) / sum by (gen_ai_request_model) (rate({SM}_count{{{OA}, gen_ai_operation_name="invoke_agent"}}[15m]))', 'in model calls, {{gen_ai_request_model}}', 'A'),
    target(f'sum(rate({SM}_sum{{{OA}, gen_ai_operation_name="execute_tool"}}[15m])) / sum(rate({SM}_count{{{OA}, gen_ai_operation_name="invoke_agent"}}[15m]))', 'in tools, all models', 'B')],
    's', 'Average seconds per run, from span durations. Model time is nearly all of it.',
    overrides=[color_override('.*gemma.*', BLUE, True), color_override('.*gemini.*', ORANGE, True), color_override('in tools, all models', AQUA)]), 6, 11, 12)
ai.add(series('Model calls per run', [
    target(f'sum by (gen_ai_request_model) (rate({SM}_count{{{OA}, gen_ai_operation_name="chat"}}[30m])) / sum by (gen_ai_request_model) (rate({SM}_count{{{OA}, gen_ai_operation_name="invoke_agent"}}[30m]))', '{{gen_ai_request_model}}')],
    'short', 'chat spans per invoke_agent span, rolling 30 minutes. Each tool round trip adds a call.', overrides=model_colors), 6, 11, 18)
Q = 'span_name="oncall_question", agent_task!=""'  # only the on-call agent writes this span
ai.add(series('p95 run time by kind of question', [
    target(f'histogram_quantile(0.95, sum by (le, agent_task) (rate({SM}_bucket{{{OA}, {Q}}}[30m])))', '{{agent_task}}')],
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

ERRORS = {'datasource': DS, 'enable': True, 'iconColor': RED, 'name': 'Model call failed, not the deadline',
          'expr': f'sum by (gen_ai_request_model, error_type) (increase({INF}_count{{{OA}, error_type!="", error_type!~"TimeoutError|AbortError"}}[2m])) > 0',
          'step': '2m', 'titleFormat': '{{gen_ai_request_model}} {{error_type}}', 'textFormat': 'model call failed', 'useValueForTime': False}
ai_json = dashboard('ai-agent-genai-otel', 'AI agent: latency, tokens and cost from @ai-sdk/otel',
                    'gen_ai.client.inference.duration from @ai-sdk/otel, plus token usage and list-price cost, for two AI SDK services: an on-call agent on two models, and a fix explainer for react-inp-blame on its live demo.',
                    ['ai-sdk', 'opentelemetry', 'gen-ai', 'golden-grot'],
                    collapse(ai.panels, {'Tokens and cost', 'The agent loop', "Spans from @ai-sdk/otel, turned into metrics by the Collector", 'Which runs, from the traces in Tempo'}),
                    annotations=[ERRORS])

suffix = '' if not UID else '.live'
with open(os.path.join(OUT, f'dashboard{suffix}.json'), 'w') as f:
    json.dump(ai_json, f, indent=2)
    f.write('\n')
print('wrote', suffix or 'portable', 'dashboard')
