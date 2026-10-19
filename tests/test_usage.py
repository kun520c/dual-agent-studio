import json
import unittest
from dual_agent.usage import parse_usage, summarize_usage


def lines(*events):return '\n'.join(json.dumps(e) for e in events)


class UsageTests(unittest.TestCase):
    def test_codex_cache_is_subset(self):
        value=parse_usage(lines(dict(type='turn.completed',usage=dict(input_tokens=81846,cached_input_tokens=72320,output_tokens=997,reasoning_output_tokens=67,cache_write_input_tokens=0))),'codex')
        self.assertEqual(value['input_tokens'],81846)
        self.assertEqual(value['uncached_input_tokens'],9526)
        self.assertEqual(value['reasoning_output_tokens'],67)
        self.assertIsNone(value['internal_model_requests'])

    def test_cursor_fixed_json_cache_excluded(self):
        value=parse_usage(lines(dict(type='result',usage=dict(inputTokens=16674,outputTokens=2751,cacheReadTokens=28672,cacheWriteTokens=0))),'cursor')
        self.assertEqual(value['input_tokens'],45346)
        self.assertEqual(value['cached_input_tokens'],28672)

    def test_missing_is_unknown_not_zero(self):
        for stdout in ['text '*1000, '{}', '{"usage":{"input_tokens":false}}']:
            value=parse_usage(stdout,'codex')
            self.assertIsNone(value['input_tokens'])
            self.assertIsNone(value['actual_cost'])

    def test_cursor_missing_cache_cannot_infer_total(self):
        value=parse_usage(lines(dict(type='result',usage=dict(inputTokens=4,outputTokens=0))),'cursor')
        self.assertIsNone(value['input_tokens'])
        self.assertEqual(value['output_tokens'],0)

    def test_cumulative_replaces_instead_of_summing(self):
        events=[dict(type='usage',usage_mode='cumulative',usage=dict(input_tokens=n,output_tokens=2)) for n in (10,20,20)]
        self.assertEqual(parse_usage(lines(*events),'codex')['input_tokens'],20)

    def test_identified_deltas_deduplicated(self):
        a=dict(type='usage',usage_mode='delta',event_id='a',usage=dict(input_tokens=3,output_tokens=1))
        b={**a,'event_id':'b'}
        self.assertEqual(parse_usage(lines(a,a,b),'codex')['input_tokens'],6)

    def test_terminal_overrides_deltas(self):
        a=dict(type='usage',usage_mode='delta',event_id='a',usage=dict(input_tokens=3,output_tokens=1))
        final=dict(type='usage',usage_mode='total',usage=dict(input_tokens=9,output_tokens=2))
        self.assertEqual(parse_usage(lines(a,final),'codex')['input_tokens'],9)

    def test_partial_totals(self):
        result=summarize_usage([dict(invoked=True,input_tokens=3),dict(invoked=True,input_tokens=None)],4)
        self.assertEqual(result['totals']['input_tokens'],dict(known=3,complete=False))
        self.assertEqual(result['cli_launches'],2)
        self.assertIsNone(result['actual_cost'])
