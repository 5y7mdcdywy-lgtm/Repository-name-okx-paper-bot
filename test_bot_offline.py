import unittest
from decimal import Decimal as D
from unittest.mock import patch

import bot


class StrategyOfflineTests(unittest.TestCase):
    def test_ema_last_two_matches_same_ema_calculation(self):
        values = [D(x) for x in ("100", "101", "99", "102", "103", "104", "102", "105", "106", "107", "108", "109", "110", "111", "112")]
        previous, latest = bot.ema_last_two(values, 5)
        self.assertEqual(latest, bot.ema(values, 5))
        self.assertEqual(previous, bot.ema(values[:-1], 5))

    def test_ema_last_two_rejects_insufficient_data(self):
        with self.assertRaises(RuntimeError):
            bot.ema_last_two([D("1"), D("2"), D("3")], 3)

    def test_rsi_is_bounded(self):
        rising = [D(100 + i) for i in range(20)]
        falling = [D(120 - i) for i in range(20)]
        self.assertEqual(bot.rsi(rising, 14), D(100))
        self.assertGreaterEqual(bot.rsi(falling, 14), D(0))
        self.assertLessEqual(bot.rsi(falling, 14), D(100))

    def test_existing_exchange_position_is_not_reversed(self):
        existing = {"short": {"pos": "0.08", "posSide": "short"}}

        def fake_positions(inst):
            return existing if inst == "BTC-USDT-SWAP" else {}

        with patch.object(bot, "load_state", return_value={"pending": None}), \\
             patch.object(bot, "check_account"), \\
             patch.object(bot, "reconcile_state"), \\
             patch.object(bot, "instrument", return_value={}), \\
             patch.object(bot, "positions", side_effect=fake_positions), \\
             patch.object(bot, "signal", return_value=None) as signal_mock:
            bot.run_once()
            self.assertEqual(signal_mock.call_count, 1)
            signal_mock.assert_called_once_with("ETH-USDT-SWAP")


if __name__ == "__main__":
    unittest.main()
