import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

HERE = pathlib.Path(__file__).parent


class DashboardTest(unittest.TestCase):
    def test_default_excludes_synthetic_shop(self):
        dashboard = json.loads((HERE / 'dashboard.json').read_text())
        self.assertEqual(dashboard['panels'][0]['title'], 'Source-backed suggestions, last hour')
        self.assertFalse(any('oncall-agent' in t.get('expr', '')
                             for p in dashboard['panels'] for t in p.get('targets', [])))
        self.assertEqual(json.loads((HERE / 'reference.json').read_text())['uid'], 'ai-agent-genai-reference')

    @unittest.skipUnless(shutil.which('promtool') or os.environ.get('PROMTOOL'), 'promtool required for PromQL evaluation')
    def test_promql_states(self):
        dashboard = json.loads((HERE / 'dashboard.json').read_text())
        expr = next(p for p in dashboard['panels'] if p['title'] == 'Source-backed suggestions, last hour')['targets'][0]['expr']
        fraction = next(p for p in dashboard['panels'] if p['title'].startswith('Whole runs passing /'))['targets'][0]['expr']
        tests = []
        def case(name, series, state, ratio=None):
            queries = [{'expr': expr, 'eval_time': '60m', 'exp_samples': [{'labels': '{}', 'value': state}]}]
            queries.append({'expr': fraction, 'eval_time': '60m', 'exp_samples':
                            [] if ratio is None else [{'labels': '{}', 'value': ratio}]})
            tests.append({'name': name, 'interval': '1m', 'input_series': series, 'promql_expr_test': queries})
        def metric(name, values):
            return {'series': name, 'values': values}
        runs = [metric('inp_explainer_runs_total{outcome="passed"}', '0+1x60'),
                metric('inp_explainer_runs_total{outcome="evaluation_failed"}', '0+0x60')]
        fresh = [metric('inp_explainer_last_completed_seconds', '0+60x60'),
                 metric('inp_explainer_heartbeat_seconds', '0+60x60')]
        case('never received samples', [], 0)
        case('all attempts pass', runs + fresh, 3, 1)
        case('source or other evaluation failure', [runs[0], metric('inp_explainer_runs_total{outcome="evaluation_failed"}', '0+1x60')] + fresh, 2, 0.5)
        case('timeouts reduce the run fraction', runs + [metric('inp_explainer_runs_total{outcome="timeout"}', '0+1x60')] + fresh, 2, 0.5)
        case('old answer with a live exporter', runs + [metric('inp_explainer_last_completed_seconds', '60+0x60'), fresh[1]], 1, 1)
        case('exporter replays old timestamps', runs + [fresh[0], metric('inp_explainer_heartbeat_seconds', '60+0x60')], 1, 1)
        case('zero runs is not success', [metric('inp_explainer_runs_total{outcome="passed"}', '0+0x60')] + fresh, 1)
        case('stopped service with historical answers', [metric('inp_explainer_last_completed_seconds', '0+60x40 stale _x19')], 1)
        token_queries = [next(p for p in dashboard['panels'] if p['title'] == f'{kind.capitalize()} token estimate, last hour')['targets'][0]['expr']
                         for kind in ('input', 'output')]
        for name, series, expected in (
            ('token totals exclude the reference service and separate input/output', [
                metric('gen_ai_client_token_usage_sum{service_name="react-inp-blame-explainer", gen_ai_token_type="input", gen_ai_request_model="a"}', '0+100x60'),
                metric('gen_ai_client_token_usage_sum{service_name="react-inp-blame-explainer", gen_ai_token_type="input", gen_ai_request_model="b"}', '0+50x60'),
                metric('gen_ai_client_token_usage_sum{service_name="react-inp-blame-explainer", gen_ai_token_type="output"}', '0+10x60'),
                metric('gen_ai_client_token_usage_sum{service_name="oncall-agent", gen_ai_token_type="input"}', '0+10000x60'),
            ], [9000, 600]),
            ('no token samples stays empty', [], [None, None]),
            ('zero observed token increase stays zero', [
                metric('gen_ai_client_token_usage_sum{service_name="react-inp-blame-explainer", gen_ai_token_type="input"}', '0+0x60'),
            ], [0, None]),
        ):
            tests.append({'name': name, 'interval': '1m', 'input_series': series, 'promql_expr_test': [
                {'expr': expr, 'eval_time': '60m', 'exp_samples': [] if value is None else [{'labels': '{}', 'value': value}]}
                for expr, value in zip(token_queries, expected)]})
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / 'rules.json'
            path.write_text(json.dumps({'rule_files': [], 'evaluation_interval': '1m', 'tests': tests}))
            result = subprocess.run([os.environ.get('PROMTOOL', 'promtool'), 'test', 'rules', str(path)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
