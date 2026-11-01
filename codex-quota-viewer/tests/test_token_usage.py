import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from codexquota.providers import AccountInfo, QuotaWindow, Snapshot
from codexquota.token_usage import TokenCounts, TokenEvent, TokenScanner, _counts, estimate_tokens

NOW = 2_000_000


def window(used=30, reset=NOW+1000, limit='codex'):
    return QuotaWindow(kind='5h', slot='primary', limit_id=limit, window_minutes=300, used_percent=used, resets_at=reset)


def snapshot(used=30, reset=NOW+1000):
    return Snapshot(ok=True, account=AccountInfo(plan_type='plus'), windows=[window(used, reset)])


def event(index, used, total, *, ts=None, reset=NOW+1000, plan='plus', limit='codex'):
    return TokenEvent(ts if ts is not None else NOW-300+index*100, str(index), TokenCounts(total, total, 0, 0), [window(used, reset, limit)], plan)


class EstimateTests(unittest.TestCase):
    def test_calibration_uses_interval_and_current_quota(self):
        result = estimate_tokens(snapshot(), [event(0, 20, 1000), event(1, 25, 2000), event(2, 30, 4000)], now=NOW).windows['5h']
        self.assertEqual(result.observed_tokens, 7000)
        self.assertEqual(result.tokens_per_percent, 600)
        self.assertEqual(result.estimated_used, 18000)
        self.assertEqual(result.estimated_remaining, 42000)

    def test_copied_fork_event_is_counted_once_even_if_last_copy_has_zero_delta(self):
        original = event(0, 20, 1000)
        duplicate = replace(original, counts=TokenCounts())
        report = estimate_tokens(snapshot(), [original, duplicate], now=NOW)
        self.assertEqual(report.windows['5h'].observed_tokens, 1000)

    def test_small_percent_change_does_not_generate_large_prediction(self):
        events = [event(0, 20, 1000), event(1, 21, 2000), event(2, 22, 4000)]
        self.assertIsNone(estimate_tokens(snapshot(), events, now=NOW).windows['5h'].estimated_remaining)

    def test_rate_only_records_do_not_claim_zero_token_usage(self):
        result = estimate_tokens(snapshot(), [event(0, 20, 0), event(1, 25, 0), event(2, 30, 0)], now=NOW).windows['5h']
        self.assertIsNone(result.observed_tokens)
        self.assertIsNone(result.estimated_remaining)

    def test_future_events_and_foreign_plans_are_excluded(self):
        report = estimate_tokens(snapshot(), [event(0, 20, 1000, ts=NOW+1), event(1, 20, 2000, plan='pro')], now=NOW)
        self.assertIsNone(report.windows['5h'].observed_tokens)

    def test_other_bucket_and_prior_natural_cycle_are_excluded(self):
        report = estimate_tokens(snapshot(), [event(0, 20, 1000, reset=NOW-1000), event(1, 20, 2000, limit='other-model')], now=NOW)
        self.assertIsNone(report.windows['5h'].observed_tokens)

    def test_manual_reset_discards_previous_calibration(self):
        events = [event(0, 80, 1000), event(1, 90, 2000), event(2, 0, 3000), event(3, 1, 4000)]
        result = estimate_tokens(snapshot(1), events, now=NOW).windows['5h']
        self.assertEqual(result.observed_tokens, 4000)
        self.assertIsNone(result.estimated_remaining)

    def test_expired_window_is_not_predicted(self):
        self.assertIsNone(estimate_tokens(snapshot(reset=NOW), [event(0, 20, 1000)], now=NOW).windows['5h'].estimated_used)

    def test_stale_sample_or_new_reset_rejects_calibration(self):
        events = [event(i, 20+i*5, 1000, ts=NOW-4000+i*100) for i in range(3)]
        self.assertIsNone(estimate_tokens(snapshot(), events, now=NOW).windows['5h'].estimated_remaining)
        recent = [event(i, 20+i*5, 1000) for i in range(3)]
        self.assertIsNone(estimate_tokens(snapshot(1), recent, now=NOW).windows['5h'].estimated_remaining)

    def test_exhausted_quota_has_zero_estimated_remaining(self):
        events = [event(i, 85+i*5, 1000) for i in range(3)]
        self.assertEqual(estimate_tokens(snapshot(100), events, now=NOW).windows['5h'].estimated_remaining, 0)

    def test_cached_and_reasoning_tokens_are_not_added_twice(self):
        value = _counts(dict(input_tokens=1000, cached_input_tokens=800, output_tokens=100, reasoning_output_tokens=80, total_tokens=1100))
        self.assertEqual(value, TokenCounts(1100, 1000, 800, 100))
        self.assertIsNone(_counts(dict(input_tokens=float('nan'))))


class ScannerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name)/'rollout-test.jsonl'
        self.scanner = TokenScanner()
        self.write([dict(type='session_meta', payload=dict(model_provider='openai'))])

    def tearDown(self):
        self.directory.cleanup()

    def write(self, records):
        with self.path.open('a', encoding='utf-8') as stream:
            for record in records:
                stream.write(json.dumps(record)+'\n')

    def record(self, index, total, last, used=20):
        counts = dict(input_tokens=total, cached_input_tokens=0, output_tokens=0, total_tokens=total)
        return dict(type='event_msg', timestamp=NOW-300+index*100,
                    payload=dict(type='token_count', info=dict(total_token_usage=counts, last_token_usage=dict(input_tokens=last)),
                                 rate_limits=dict(limit_id='codex', plan_type='plus', primary=dict(used_percent=used, window_minutes=300, resets_at=NOW+1000))))

    def scan(self, paths=None):
        return self.scanner.scan(snapshot(), paths=paths or [self.path], now=NOW).windows['5h']

    def test_incremental_read_and_duplicate_counter(self):
        self.write([self.record(0, 1000, 1000), self.record(1, 3000, 2000, 25)])
        self.assertEqual(self.scan().observed_tokens, 3000)
        self.assertEqual(self.scan().observed_tokens, 3000)
        self.write([self.record(2, 7000, 4000, 30), self.record(3, 7000, 4000, 30)])
        self.assertEqual(self.scan().observed_tokens, 7000)

    def test_first_inherited_counter_and_restart_only_count_current_request(self):
        self.write([self.record(0, 500000, 1000), self.record(1, 500, 500)])
        self.assertEqual(self.scan().observed_tokens, 1500)

    def test_forked_file_does_not_duplicate_usage(self):
        self.write([self.record(0, 1000, 1000), self.record(1, 3000, 2000)])
        fork = self.path.with_name('rollout-fork.jsonl')
        fork.write_bytes(self.path.read_bytes())
        self.assertEqual(self.scan([self.path, fork]).observed_tokens, 3000)

    def test_partial_last_line_is_read_after_writer_finishes(self):
        text = json.dumps(self.record(0, 1000, 1000))
        with self.path.open('a', encoding='utf-8') as stream:
            stream.write(text[:60])
        self.assertIsNone(self.scan().observed_tokens)
        with self.path.open('a', encoding='utf-8') as stream:
            stream.write(text[60:]+'\n')
        self.assertEqual(self.scan().observed_tokens, 1000)

    def test_non_openai_provider_is_excluded(self):
        self.path.write_text(json.dumps(dict(type='session_meta', payload=dict(model_provider='custom')))+'\n', encoding='utf-8')
        self.write([self.record(0, 1000, 1000)])
        self.assertIsNone(self.scan().observed_tokens)

    def test_model_metadata_tracks_changes_and_incremental_reads(self):
        self.write([dict(type='turn_context', payload=dict(model='gpt-6-sol')),
                    self.record(0, 1000, 1000)])
        self.assertEqual(self.scan().model_usage[0].model, 'gpt-6-sol')
        self.write([dict(type='turn_context', payload=dict(model='gpt-6.1-sol')),
                    self.record(1, 3000, 2000, 25)])
        groups = self.scan().model_usage
        self.assertEqual([(g.model, g.counts.total) for g in groups], [('gpt-6-sol', 1000), ('gpt-6.1-sol', 2000)])
        self.assertFalse(groups[0].long_context)

    def test_cached_writes_and_long_request_metadata_are_preserved(self):
        record = self.record(0, 300000, 300000)
        record['payload']['info']['last_token_usage']['cache_write_tokens'] = 10000
        self.write([dict(type='turn_context', payload=dict(model='gpt-6.1-sol')), record])
        group = self.scan().model_usage[0]
        self.assertTrue(group.long_context)
        self.assertEqual(group.counts.cache_write, 10000)

    def test_file_truncation_resets_offset_and_counters(self):
        self.write([self.record(0, 100000, 100000), self.record(1, 200000, 100000)])
        self.assertEqual(self.scan().observed_tokens, 200000)
        self.path.write_text(json.dumps(dict(type='session_meta', payload=dict(model_provider='openai')))+'\n', encoding='utf-8')
        self.write([self.record(2, 500, 500)])
        self.assertEqual(self.scan().observed_tokens, 500)


class SessionTests(ScannerTests):
    def report(self, paths=None):
        return self.scanner.scan(snapshot(), paths=paths or [self.path], now=NOW)

    def test_session_cache_breakdown_and_serialization(self):
        record = self.record(0, 1100, 1100)
        for key in ('total_token_usage', 'last_token_usage'):
            record['payload']['info'][key] = dict(input_tokens=1000, cached_input_tokens=800,
                                                  output_tokens=100, total_tokens=1100)
        self.write([record])
        report = self.report()
        self.assertEqual(report.session.observed_tokens, 1100)
        self.assertEqual(report.session.input_tokens, 1000)
        self.assertEqual(report.session.uncached_input_tokens, 200)
        self.assertEqual(report.session.output_tokens, 100)
        self.assertEqual(report.session.cache_hit_rate, .8)
        self.assertEqual(report.to_dict()['session']['cache_hit_rate'], .8)
        self.assertEqual(report.to_dict()['windows']['5h']['uncached_input_tokens'], 200)

    def test_session_tokens_do_not_require_quota_response(self):
        record = self.record(0, 1000, 1000)
        del record['payload']['rate_limits']
        self.write([record])
        self.assertEqual(self.report().session.observed_tokens, 1000)
        self.assertIsNone(self.report().windows['5h'].observed_tokens)

    def test_latest_activity_beats_file_mtime_and_new_empty_session_is_not_old_usage(self):
        self.write([self.record(0, 1000, 1000)])
        other = self.path.with_name('newer-session.jsonl')
        other.write_text(json.dumps(dict(type='session_meta', timestamp=NOW-10,
            payload=dict(id='new-session', model_provider='openai')))+'\n', encoding='utf-8')
        report = self.report([self.path, other])
        self.assertEqual(report.session_id, 'new-session')
        self.assertIsNone(report.session.observed_tokens)

    def test_session_report_is_immutable_after_incremental_scan(self):
        self.write([self.record(0, 1000, 1000)])
        previous = self.report()
        self.write([self.record(1, 3000, 2000)])
        self.assertEqual(self.report().session.observed_tokens, 3000)
        self.assertEqual(previous.session.observed_tokens, 1000)

    def test_session_survives_quota_reset_and_counter_restart(self):
        self.write([self.record(0, 1000, 1000, 90), self.record(1, 3000, 2000, 0),
                    self.record(2, 500, 500, 1)])
        self.assertEqual(self.report().session.observed_tokens, 3500)

    def test_inherited_history_before_session_start_is_not_session_consumption(self):
        self.path.write_text(json.dumps(dict(type='session_meta', timestamp=NOW-150,
            payload=dict(id='fork', model_provider='openai')))+'\n', encoding='utf-8')
        self.write([self.record(0, 1000, 1000), self.record(2, 3000, 2000)])
        self.assertEqual(self.report().session.observed_tokens, 2000)

    def test_zero_input_has_no_fabricated_hit_rate(self):
        from codexquota.token_usage import TokenEstimate
        self.assertIsNone(TokenEstimate(output_tokens=100).cache_hit_rate)


if __name__ == '__main__':
    unittest.main()
