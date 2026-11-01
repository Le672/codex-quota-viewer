import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codexquota.appenv import Settings
from codexquota.pricing import PRICES, estimate_value, money, normalize_model
from codexquota.token_usage import ModelUsage, TokenCounts, TokenEstimate


def sample(model='gpt-6.1-sol', *, writes=None, long=False):
    counts = TokenCounts(1_100_000, 1_000_000, 800_000, 100_000, writes)
    return ModelUsage(model, counts, long, 1)


def estimate(*groups, used=2_200_000, remaining=1_100_000):
    return TokenEstimate(observed_tokens=sum(g.counts.total for g in groups),
                         estimated_used=used, estimated_remaining=remaining, model_usage=list(groups))


class PricingTests(unittest.TestCase):
    def test_cached_input_is_discounted_and_not_counted_twice(self):
        result = estimate_value(estimate(sample()))
        self.assertAlmostEqual(result.observed_usd, 1.48)
        self.assertAlmostEqual(result.used_usd, 2.96)
        self.assertAlmostEqual(result.remaining_usd, 1.48)
        self.assertIn('非订阅账单', result.note)

    def test_known_cache_writes_replace_ordinary_input(self):
        self.assertAlmostEqual(estimate_value(estimate(sample(writes=100_000))).observed_usd, 1.53)
        self.assertIn('没有缓存写入分类', estimate_value(estimate(sample())).note)

    def test_mixed_models_are_priced_separately(self):
        value = estimate_value(estimate(sample(), sample('gpt-5.6-sol')))
        self.assertAlmostEqual(value.observed_usd, 4.60)
        self.assertIn('gpt-5.6-sol', value.basis)

    def test_manual_model_overrides_detected_and_unknown_models(self):
        value = estimate_value(estimate(sample(), sample('unknown')), 'gpt-5.6-sol')
        self.assertAlmostEqual(value.observed_usd, 6.24)
        self.assertTrue(value.basis.startswith('指定模型'))

    def test_unknown_model_never_looks_like_zero_cost(self):
        value = estimate_value(estimate(sample(), sample('future-model')))
        self.assertAlmostEqual(value.observed_usd, 1.48)
        self.assertAlmostEqual(value.used_usd, 2.96)
        self.assertEqual(value.unpriced_tokens, 1_100_000)
        self.assertIn('future-model', value.note)
        unknown = estimate_value(estimate(sample('future-model')))
        self.assertIsNone(unknown.used_usd)
        self.assertIsNone(unknown.remaining_usd)

    def test_partial_observed_value_is_a_lower_bound(self):
        value = estimate_value(estimate(sample(), sample('unknown'), used=None, remaining=None))
        self.assertTrue(value.used_is_lower_bound)
        self.assertAlmostEqual(value.used_usd, 1.48)

    def test_total_only_or_inconsistent_counts_do_not_create_price(self):
        value = estimate_value(TokenEstimate(observed_tokens=1000), 'gpt-6.1-sol')
        self.assertIsNone(value.used_usd)
        broken = sample()
        broken.counts = TokenCounts(1, 100, 99, 0, 20)
        self.assertIsNone(estimate_value(estimate(broken)).used_usd)

    def test_exhausted_quota_is_zero_and_unavailable_prediction_stays_unknown(self):
        self.assertEqual(estimate_value(estimate(sample(), remaining=0)).remaining_usd, 0)
        value = estimate_value(estimate(sample(), used=None, remaining=None))
        self.assertAlmostEqual(value.used_usd, 1.48)
        self.assertIsNone(value.remaining_usd)
        self.assertIsNone(estimate_value(None).used_usd)

    def test_long_context_uses_input_cache_and_output_multipliers(self):
        self.assertAlmostEqual(estimate_value(estimate(sample(long=True))).observed_usd, 2.46)
        old = sample('gpt-5.3-codex', long=True)
        self.assertAlmostEqual(estimate_value(estimate(old)).observed_usd, 1.89)

    def test_only_documented_aliases_and_dated_snapshots_resolve(self):
        self.assertEqual(normalize_model('gpt-5.6'), 'gpt-5.6-sol')
        self.assertEqual(normalize_model('gpt-5.5-2026-04-23'), 'gpt-5.5')
        self.assertNotIn(normalize_model('gpt-6-new'), PRICES)

    def test_money_retains_small_nonzero_values(self):
        self.assertEqual(money(None), '—')
        self.assertEqual(money(0), '$0.00')
        self.assertEqual(money(.002), '$0.002')
        self.assertEqual(money(.0001), '<$0.001')
        self.assertEqual(money(float('nan')), '—')

    def test_price_settings_clamp_and_round_trip(self):
        settings = Settings(pricing_model='invalid', show_token_value='false', click_through='true')
        settings.clamp()
        self.assertEqual(settings.pricing_model, 'auto')
        self.assertFalse(settings.show_token_value)
        self.assertTrue(settings.click_through)
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', APPDATA=directory):
            settings.pricing_model = 'gpt-6.1-sol'
            settings.save()
            loaded = Settings.load()
            self.assertEqual(loaded.pricing_model, 'gpt-6.1-sol')
            self.assertFalse(loaded.show_token_value)

    def test_value_json_keeps_source_currency_and_date(self):
        payload = json.loads(json.dumps(estimate_value(estimate(sample())).to_dict()))
        self.assertEqual(payload['currency'], 'USD')
        self.assertEqual(payload['pricing_checked'], '2026-10-03')
        self.assertTrue(payload['pricing_url'].startswith('https://developers.openai.com/'))


if __name__ == '__main__':
    unittest.main()
