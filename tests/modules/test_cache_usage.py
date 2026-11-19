import unittest
from bench.cache_usage import cache_usage, cache_metrics


class CacheUsageTest(unittest.TestCase):
    def test_missing_zero_and_invalid_usage_are_distinct(self):
        self.assertEqual(cache_usage({}), (None, None))
        self.assertEqual(
            cache_usage(
                {"prompt_tokens": 10, "prompt_tokens_details": {"cached_tokens": 0}}
            ),
            (10, 0),
        )
        self.assertEqual(
            cache_usage(
                {"prompt_tokens": 10, "cached_tokens": 0, "prompt_cache_hit_tokens": 5}
            ),
            (10, 0),
        )
        self.assertEqual(
            cache_usage({"prompt_tokens": 10, "cached_tokens": 11}), (10, None)
        )

    def test_hit_rate_uses_only_matched_usage(self):
        result = cache_metrics(
            [
                {"prompt_tokens": 10, "cached_tokens": 5},
                {"prompt_tokens": 90, "cached_tokens": None},
                {"prompt_tokens": 20, "cached_tokens": 0},
            ]
        )
        self.assertEqual(result["cache_hit_rate"], 5 / 30)
        self.assertEqual(result["cache_usage_coverage"], 2 / 3)
        self.assertIsNone(cache_metrics([{"prompt_tokens": 10}])["cache_hit_rate"])
