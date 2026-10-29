import unittest
from datetime import date

from backtest import battery_model, build_days, run_backtest, synthetic_samples


class BacktestTests(unittest.TestCase):
    def test_synthetic_backtest_orders_strategies_by_information(self):
        days = build_days(synthetic_samples(4, first_day=date(2026, 10, 1)), True)
        self.assertEqual(len(days), 4)
        result = run_backtest(days, battery_model(5, 0.01), True)

        self.assertGreaterEqual(result["perfect"]["total_savings_eur"], -0.01)
        self.assertGreaterEqual(
            result["perfect_hold"]["total_savings_eur"],
            result["perfect"]["total_savings_eur"] - 1e-6,
        )
        self.assertEqual(result["persistence"]["days"], 3)

    def test_incomplete_days_are_skipped(self):
        samples = synthetic_samples(2, first_day=date(2026, 10, 1))
        partial = [item for item in samples if not (item[0].day == 2 and item[0].hour == 10)]
        self.assertEqual(len(build_days(partial, True)), 1)


if __name__ == "__main__":
    unittest.main()
