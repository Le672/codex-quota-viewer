"""验证不依赖界面的后端行为并保存当前版本报告。"""
import json
import sys
import unittest
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
sys.path.insert(0, str(root / 'tests'))
from codexquota.appenv import APP_VERSION

suite = unittest.defaultTestLoader.loadTestsFromNames([
    'test_community_api', 'test_regressions', 'test_token_usage', 'test_pricing',
    'test_lifecycle', 'test_shared_data', 'test_placement'])
result = unittest.TextTestRunner(verbosity=1).run(suite)
report = dict(ok=result.wasSuccessful(), tests=result.testsRun,
              failures=len(result.failures), errors=len(result.errors))
(root / f'v{APP_VERSION}-backend-validation.json').write_text(
    json.dumps(report), encoding='utf-8')
print(json.dumps(report))
sys.exit(0 if result.wasSuccessful() else 1)
