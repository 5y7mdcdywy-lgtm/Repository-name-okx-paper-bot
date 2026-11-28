import unittest
from decimal import Decimal as D
from unittest.mock import patch

import altcoin_bot


class AltcoinOfflineTests(unittest.TestCase):
    def test_sol_is_excluded_from_entries_but_kept_for_position_management(self):
        self.assertNotIn("SOL-USDT-SWAP", altcoin_bot.SYMBOLS)
        self.assertIn("SOL-USDT-SWAP", altcoin_bot.MANAGED_SYMBOLS)

    def test_volume_expansion_accepts_at_least_110_percent(self):
        volumes = [D("100")] * 20 + [D("110")]
        self.assertTrue(altcoin_bot.volume_expansion(volumes))

    def test_volume_expansion_rejects_weak_or_missing_volume(self):
        self.assertFalse(altcoin_bot.volume_expansion([D("100")] * 20 + [D("109")]))
        self.assertFalse(altcoin_bot.volume_expansion([D("100")] * 20))
        self.assertFalse(altcoin_bot.volume_expansion([D("0")] * 21))

    def test_candle_parser_uses_only_confirmed_candles_and_quote_volume(self):
        rows = [
            ["3000", "1", "2", "1", "1.5", "10", "10", "150", "1"],
            ["2000", "1", "2", "1", "1.4", "10", "10", "140", "1"],
            ["4000", "1", "2", "1", "1.6", "10", "10", "160", "0"],
        ]
        with patch.object(altcoin_bot, "get", return_value=rows):
            closes, volumes = altcoin_bot.candles_with_quote_volume(
                "SOL-USDT-SWAP", "5m", 100
            )
        self.assertEqual(closes, [D("1.4"), D("1.5")])
        self.assertEqual(volumes, [D("140"), D("150")])

    def test_signal_skips_market_without_volume_expansion(self):
        closes = [D(100 + i) for i in range(50)]
        volumes = [D("100")] * 21
        with (
            patch.object(altcoin_bot, "candles_with_quote_volume",
                         return_value=(closes, volumes)),
            patch.object(altcoin_bot, "candles",
                         return_value=closes),
            patch.object(altcoin_bot, "ema") as ema_mock,
        ):
            self.assertIsNone(altcoin_bot.signal("SOL-USDT-SWAP"))
            ema_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
