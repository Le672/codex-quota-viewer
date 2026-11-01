"""共享目录避免 MSIX 副本；隔离运行和迁移必须保留已有偏好。"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codexquota.appenv import APP_NAME, app_data_dir


class SharedDataTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.profile = Path(self.temporary.name) / 'profile'
        self.roaming = self.profile / 'AppData' / 'Roaming'
        self.legacy = self.roaming / APP_NAME
        self.shared = self.profile / '.codex-quota-viewer'
        self.legacy.mkdir(parents=True)
        self.shared.mkdir(parents=True)
        (self.shared / 'storage.json').write_text('{"schema":1}', encoding='utf-8')
        self.environment = patch.dict(os.environ, USERPROFILE=str(self.profile),
                                      APPDATA=str(self.roaming), CODEXQUOTA_INSTANCE_SCOPE='')
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_shared_migrates_own_settings_and_cache_without_credentials(self):
        settings = b'{"compact":true,"material":"transparent"}'
        (self.legacy / 'settings.json').write_bytes(settings)
        (self.legacy / 'community-api-v1.json').write_text('{"ok":true}', encoding='utf-8')
        (self.legacy / 'auth.json').write_text('do not copy', encoding='utf-8')
        self.assertEqual(app_data_dir(), self.shared)
        self.assertEqual((self.shared / 'settings.json').read_bytes(), settings)
        self.assertTrue((self.shared / 'community-api-v1.json').is_file())
        self.assertFalse((self.shared / 'auth.json').exists())
        self.assertEqual((self.legacy / 'settings.json').read_bytes(), settings)

    def test_shared_never_overwrites_current_preferences(self):
        (self.legacy / 'settings.json').write_text('{"compact":false}', encoding='utf-8')
        saved = b'{"compact":true,"transparent_opacity":0.2}'
        (self.shared / 'settings.json').write_bytes(saved)
        self.assertEqual(app_data_dir(), self.shared)
        self.assertEqual((self.shared / 'settings.json').read_bytes(), saved)

    def test_explicit_appdata_and_isolated_scope_remain_isolated(self):
        alternate = Path(self.temporary.name) / 'explicit-data'
        with patch.dict(os.environ, APPDATA=str(alternate)):
            self.assertEqual(app_data_dir(), alternate / APP_NAME)
        with patch.dict(os.environ, CODEXQUOTA_INSTANCE_SCOPE='test'):
            self.assertEqual(app_data_dir(), self.legacy)

    def test_uninstalled_source_keeps_legacy_location(self):
        (self.shared / 'storage.json').unlink()
        self.assertEqual(app_data_dir(), self.legacy)


if __name__ == '__main__':
    unittest.main()
