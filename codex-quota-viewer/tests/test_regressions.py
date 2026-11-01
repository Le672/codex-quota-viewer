"""关键运行问题的回归验证（不使用账号、不访问网络、不消耗重置卡）。"""
import json
import queue
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from codexquota.appenv import Settings
from codexquota.collector import Bundle, DataController
from codexquota.providers import (AuthFile, CodexClient, QuotaError, Snapshot,
                                  latest_local_usage, parse_credits, parse_usage)


class Regressions(unittest.TestCase):
    def test_official_camel_case(self):
        windows, meta = parse_usage({"rateLimitsByLimitId": {"codex": {
            "primary": {"usedPercent": 21, "windowDurationMins": 300,
                        "resetsAt": 1900000000},
            "secondary": {"usedPercent": 76, "windowDurationMins": 10080}}},
            "credits": {"hasCredits": True, "balance": "4"}, "planType": "plus"})
        self.assertEqual([(w.window_minutes, w.used_percent) for w in windows],
                         [(300, 21), (10080, 76)])
        self.assertEqual(meta["has_credits"], True)

    def test_http_windows_seconds(self):
        windows, _ = parse_usage({"rate_limit": {
            "primary_window": {"used_percent": 5, "limit_window_seconds": 18000},
            "secondary_window": {"used_percent": 10, "limit_window_seconds": 604800}}})
        self.assertEqual([w.window_minutes for w in windows], [300, 10080])

    def test_missing_invalid_usage_is_not_zero(self):
        for value in (None, "oops", "NaN", "Infinity", True):
            with self.subTest(value=value):
                windows, _ = parse_usage({"rate_limits": {
                    "primary": {"used_percent": value, "window_minutes": 300}}})
                self.assertEqual(windows, [])

    def test_zero_usage_is_valid(self):
        windows, _ = parse_usage({"rate_limits": {
            "primary": {"used_percent": 0, "window_minutes": 300}}})
        self.assertEqual(windows[0].remaining_percent, 100)

    def test_reset_cards_filter_expired_and_duplicates(self):
        cards = parse_credits({"rateLimitResetCredits": {"credits": [
            {"id": "ok", "expiresAt": 200, "resetType": "codexRateLimits"},
            {"id": "ok", "expiresAt": 200}, {"id": "expired", "expiresAt": 99},
            {"id": "used", "status": "consumed"}]}}, now=100)
        self.assertEqual([c.credit_id for c in cards], ["ok"])
        self.assertEqual(cards[0].scope, "codexRateLimits")

    def test_refresh_releases_busy_and_runs_again(self):
        events = queue.Queue()
        controller = DataController(Settings(), events)
        with patch.object(controller, "_collect", return_value=Bundle(snapshot=Snapshot(ok=True))):
            for _ in range(2):
                self.assertTrue(controller.refresh_async())
                while events.get(timeout=2)["type"] != "refresh_done":
                    pass
                self.assertFalse(controller.busy)

    def test_refresh_exception_releases_busy(self):
        events = queue.Queue()
        controller = DataController(Settings(), events)
        with patch.object(controller, "_collect", side_effect=RuntimeError("fixture error")), \
                patch("codexquota.collector.log"):
            controller.refresh_async()
            while True:
                event = events.get(timeout=2)
                if event["type"] == "refresh_done":
                    break
            self.assertFalse(controller.busy)
            self.assertFalse(event["bundle"].snapshot.ok)

    def test_unavailable_reset_endpoint_not_empty_success(self):
        client = CodexClient(AuthFile(Path("fixture.json"), {}))
        with patch.object(client, "_get_json", return_value=(404, {})):
            with self.assertRaises(QuotaError):
                client.fetch_reset_credits()

    def test_embedded_count_still_fetches_actual_cards(self):
        controller = DataController(Settings(), queue.Queue())
        client = CodexClient(AuthFile(Path("fixture.json"), {}))
        payload = {"rate_limit": {"primary_window": {
            "used_percent": 20, "limit_window_seconds": 18000}},
            "rate_limit_reset_credits": {"available_count": 2}}
        with patch.object(client, "fetch_usage", return_value=payload), \
                patch.object(client, "fetch_reset_credits", return_value=[]) as fetch:
            controller._fetch_api(client, client.info, [])
            fetch.assert_called_once()

    def test_tray_v4_event_uses_lparam(self):
        from codexquota.tray import TrayIcon, WM_TRAYICON, WM_LBUTTONUP
        calls = []
        tray = TrayIcon(None, on_show=lambda: calls.append("show"))
        tray._handle_message(0, WM_TRAYICON, 0x12341234, (1 << 16) | WM_LBUTTONUP)
        self.assertEqual(calls, ["show"])

    def test_403_does_not_rotate_tokens(self):
        client = CodexClient(AuthFile(Path("fixture.json"), {}))
        with patch.object(client, "_headers", return_value={}), \
                patch.object(client, "token") as token, \
                patch("codexquota.providers.http_request", return_value=(403, {})):
            client._get_json("/fixture")
            token.assert_not_called()

    def test_latest_snapshot_uses_event_time_not_file_order(self):
        with tempfile.TemporaryDirectory() as directory:
            files = [Path(directory) / name for name in ("newly-touched.jsonl", "recent-event.jsonl")]
            for file, stamp, used in zip(files, (1900000000, 1900000100), (10, 20)):
                file.write_text(json.dumps({"timestamp": stamp, "payload": {"rate_limits": {
                    "primary": {"used_percent": used, "window_minutes": 300}}}}), encoding="utf-8")
            with patch("codexquota.providers.newest_session_files", return_value=files):
                self.assertEqual(latest_local_usage()[0]["primary"]["used_percent"], 20)

    def test_empty_history_is_cached(self):
        controller = DataController(Settings(), queue.Queue())
        with patch("codexquota.collector.collect_history", return_value=[]) as collect:
            controller._get_history()
            controller._get_history()
            collect.assert_called_once()

    def test_broken_settings_are_clamped(self):
        settings = Settings(warn_percent="oops", expiry_warn_days="NaN",
                            community_ttl=-100, use_api="false", geometry=10)
        settings.clamp()
        self.assertEqual(settings.warn_percent, 85)
        self.assertEqual(settings.expiry_warn_days, 7)
        self.assertEqual(settings.community_ttl, 1)
        self.assertFalse(settings.use_api)
        self.assertEqual(settings.geometry, "")

    def test_opacity_limits_and_non_finite_values(self):
        for opacity, expected in ((-1, 0), (2, 1), (.37, .37), ("NaN", 0), ("inf", 0), (None, 0)):
            with self.subTest(opacity=opacity):
                settings = Settings(transparent_opacity=opacity, opacity="NaN")
                settings.clamp()
                self.assertEqual(settings.transparent_opacity, expected)
                self.assertEqual(settings.opacity, .62)

    def test_custom_colors_accept_only_known_hex_roles(self):
        settings = Settings(hide_taskbar="false", custom_colors={
            "text": "#123abc", "weekly": "#FF0088", "warn": "red; background: black",
            "muted": None, "unknown": "#FFFFFF"})
        settings.clamp()
        self.assertFalse(settings.hide_taskbar)
        self.assertEqual(settings.custom_colors, {"text": "#123ABC", "weekly": "#FF0088"})
        settings.custom_colors = []
        settings.clamp()
        self.assertEqual(settings.custom_colors, {})

    def test_appearance_settings_survive_restart(self):
        with tempfile.TemporaryDirectory(prefix="quota-settings-") as directory, \
                patch.dict("os.environ", {"APPDATA": directory}):
            settings = Settings(always_on_top=True, hide_taskbar=True,
                                transparent_opacity=.43, custom_colors={"title": "#AA88CC"})
            settings.save()
            restored = Settings.load()
            self.assertTrue(restored.always_on_top and restored.hide_taskbar)
            self.assertEqual(restored.transparent_opacity, .43)
            self.assertEqual(restored.custom_colors, {"title": "#AA88CC"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
