"""公开 API 的事件语义、缓存、撤回更正与过期状态回归。"""
import sys
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from codexquota import community_api as api, events as ev

NOW = 1790748000
def iso(stamp):
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat()
def event(identity, kind='direct_reset', days=1):
    return dict(id=identity, type=kind, status='confirmed', confirmedAt=iso(NOW-days*86400),
                updatedAt=iso(NOW-days*86400), createdAt=iso(NOW-days*86400),
                presentation=dict(status='confirmed', productsZh='Codex'), scope='所有付费用户')
def payload(items):
    return dict(schemaVersion=1, checkedAt=iso(NOW), historyFrom=iso(NOW-7*86400),
                events=items, monitor=dict(status='healthy'))

class CommunityAPI(unittest.TestCase):
    def setUp(self):
        self.log_patch = patch.object(api, 'log')
        self.log_patch.start()
        self.addCleanup(self.log_patch.stop)

    def test_statistical_forecast_is_display_only(self):
        from codexquota.analytics import build_advice
        from codexquota.providers import Snapshot
        community = api.parse_api(payload([
            event('new', days=1), event('middle', days=3), event('old', days=5)]), NOW)
        self.assertIsNotNone(community.next_reset.next_event_ts)
        advice = build_advice(Snapshot(ok=True), [], now=NOW, community=community)
        self.assertTrue(advice.community_note)
        self.assertIsNone(advice.community_ts)

    def test_types_and_day_dedup(self):
        snap = api.parse_api(payload([event('one'), event('duplicate'), event('card', 'reset_credit')]), NOW)
        self.assertEqual((len(snap.resets()), len(snap.cards())), (1, 1))
        self.assertEqual(snap.resets()[0].time_precision, 'confirmation')

    def test_occurrence_date_wins_over_confirmation(self):
        item = event('date')
        item['occurredOn'] = '2026-09-23'
        snap = api.parse_api(payload([item]), NOW)
        self.assertEqual(snap.events[0].date, '2026-09-23')
        self.assertEqual(snap.events[0].time_precision, 'date')

    def test_model_estimate_is_not_official_schedule(self):
        item = event('pending')
        item.update(status='announced', confirmedAt=None, presentation=dict(status='announced'),
                    estimate=dict(**{'from': iso(NOW+3600), 'through': iso(NOW+7200)}, reason='模型推算'))
        snap = api.parse_api(payload([item]), NOW)
        self.assertEqual(snap.next_reset.source, 'aihot_estimate')
        self.assertIsNone(snap.announcement)
        self.assertEqual(snap.events, [])

    def test_expired_and_likely_completed_not_pending(self):
        item = event('old')
        item.update(status='announced', confirmedAt=None, presentation=dict(status='likely_completed'),
                    estimate={'from': iso(NOW-7200), 'through': iso(NOW-3600)})
        snap = api.parse_api(payload([item]), NOW)
        self.assertEqual(snap.pending_labels, [])
        self.assertIsNone(snap.next_reset.next_event_ts)

    def test_explicit_schedule_keeps_announcement(self):
        item = event('schedule')
        item.update(status='announced', confirmedAt=None, presentation=dict(status='announced'),
                    schedule={'from': iso(NOW+3600), 'through': iso(NOW+7200)})
        snap = api.parse_api(payload([item]), NOW)
        self.assertEqual(snap.next_reset.source, 'announcement')
        self.assertIsNotNone(snap.announcement)

    def test_recent_snapshot_applies_removal_and_correction(self):
        full = payload([event('removed'), event('changed'), event('older', days=30)])
        recent = payload([event('changed', 'reset_credit')])
        merged = api.merge_recent(full, recent)
        self.assertEqual([item['id'] for item in merged['events']], ['older', 'changed'])
        self.assertEqual(merged['events'][-1]['type'], 'reset_credit')

    def test_stale_verification_visible(self):
        data = payload([])
        data['checkedAt'] = iso(NOW-2*86400)
        data['monitor']['status'] = 'delayed'
        self.assertEqual(len(api.parse_api(data, NOW).warnings), 2)

    def test_invalid_schema_is_error(self):
        for data in ({}, {'schemaVersion': 9, 'events': []}, {'schemaVersion': 1, 'events': None}):
            with self.assertRaises(ValueError):
                api.parse_api(data, NOW)

    def test_fresh_cache_no_request_and_304_preserves_payload(self):
        state = dict(full=payload([event('one')]), recent=payload([event('one')]), full_at=NOW,
                     recent_at=NOW, recent_etag='test-etag')
        with patch.object(api, 'read_state', return_value=state), patch.object(api, 'request_api') as request:
            self.assertEqual(api.fetch_api(now=NOW+10).source, 'cache')
            request.assert_not_called()
            request.return_value = (304, None, 'test-etag')
            with patch.object(api, 'save_state'):
                snap = api.fetch_api(now=NOW+400)
            request.assert_called_once_with(ev.RECENT_API_URL, 'test-etag')
            self.assertEqual(snap.source, 'live')
            self.assertEqual(len(snap.events), 1)
            self.assertEqual(snap.api_checked_at, NOW)

    def test_failure_uses_dated_cache_and_no_fake_baseline(self):
        state = dict(full=payload([event('one')]), full_at=NOW, recent_at=NOW)
        with patch.object(api, 'read_state', return_value=state), patch.object(api, 'request_api', side_effect=TimeoutError('fixture')):
            snap = api.fetch_api(force=True, now=NOW+100)
            self.assertEqual(snap.source, 'cache')
            self.assertEqual(snap.fetched_at, NOW)
            self.assertTrue(snap.warnings)
        with patch.object(api, 'read_state', return_value={}), patch.object(api, 'request_api', side_effect=TimeoutError('fixture')):
            snap = api.fetch_api(now=NOW)
            self.assertFalse(snap.ok)
            self.assertIsNone(snap.next_reset)

if __name__ == '__main__':
    unittest.main(verbosity=2)
