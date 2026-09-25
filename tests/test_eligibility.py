import unittest
from datetime import date, timedelta

from relief_core.eligibility import DEFAULT_CRITERIA, evaluate, normalize_criteria


class CriteriaTest(unittest.TestCase):
    def test_defaults_are_complete(self):
        criteria = normalize_criteria(None)
        self.assertEqual(DEFAULT_CRITERIA, criteria)

    def test_overrides_merge(self):
        criteria = normalize_criteria({"window_duration_days": 14})
        self.assertEqual(14, criteria["window_duration_days"])
        self.assertEqual(30, criteria["min_consecutive_qualified_days"])

    def test_invalid_values(self):
        for bad in (
            {"required_qualified_ratio": 0},
            {"required_qualified_ratio": 2},
            {"window_duration_days": -1},
            {"review_interval_days": 0},
            {"require_open_hazards_closed": "yes"},
            {"min_consecutive_qualified_days": 1.5},
            {"unknown": 1},
            "not-a-dict",
        ):
            with self.assertRaises(ValueError):
                normalize_criteria(bad)


def daily(days, *, on_time=True, quality="qualified"):
    end = date(2026, 9, 25)
    return [{"date": (end - timedelta(days=days - 1 - i)).isoformat(),
             "on_time": on_time, "quality": quality} for i in range(days)]


class EvaluateTest(unittest.TestCase):
    def setUp(self):
        self.criteria = normalize_criteria({
            "min_consecutive_qualified_days": 3, "qualified_ratio_days": 3,
            "window_duration_days": 5, "review_interval_days": 5,
            "hazard_lookback_days": 30, "inspection_lookback_days": 30,
            "serious_lookback_days": 30, "assistance_lookback_days": 30})
        self.day = date(2026, 9, 25)

    def test_clean_record_is_eligible(self):
        facts = {"daily": daily(3), "hazards": [], "inspections": [], "assistances": [],
                 "serious_events": []}
        eligible, reasons, metrics = evaluate(
            self.criteria, settlement_date=self.day, consecutive_qualified_days=3, facts=facts)
        self.assertTrue(eligible)
        self.assertEqual(1.0, metrics["qualified_ratio"])
        self.assertTrue(any("门槛" in r for r in reasons))

    def test_late_report_shows_in_metrics_and_blocks(self):
        facts = {"daily": [
            {"date": "2026-09-25", "on_time": False, "quality": "qualified"},
            {"date": "2026-09-24", "on_time": True, "quality": "qualified"},
            {"date": "2026-09-23", "on_time": True, "quality": "qualified"}],
            "hazards": [], "inspections": [], "assistances": [], "serious_events": []}
        eligible, reasons, metrics = evaluate(
            self.criteria, settlement_date=self.day, consecutive_qualified_days=0, facts=facts)
        self.assertFalse(eligible)
        self.assertEqual(1, metrics["deficient_or_late_reports"])
        self.assertTrue(any("迟报" in r for r in reasons))

    def test_open_major_hazard_and_repeated_rectification_block(self):
        facts = {"daily": daily(3),
                 "hazards": [{"hazard_key": "h1", "found_date": "2026-09-24",
                              "closed_date": None, "severity": "major",
                              "rectification_count": 0},
                             {"hazard_key": "h2", "found_date": "2026-09-23",
                              "closed_date": "2026-09-24", "severity": "general",
                              "rectification_count": 4}],
                 "inspections": [], "assistances": [], "serious_events": []}
        eligible, reasons, _ = evaluate(
            self.criteria, settlement_date=self.day, consecutive_qualified_days=3, facts=facts)
        self.assertFalse(eligible)
        joined = "\n".join(reasons)
        self.assertIn("重大隐患", joined)
        self.assertIn("反复整改", joined)

    def test_closed_hazard_within_limit_passes(self):
        facts = {"daily": daily(3),
                 "hazards": [{"hazard_key": "h1", "found_date": "2026-09-20",
                              "closed_date": "2026-09-21", "severity": "general",
                              "rectification_count": 1}],
                 "inspections": [], "assistances": [], "serious_events": []}
        eligible, _, _ = evaluate(
            self.criteria, settlement_date=self.day, consecutive_qualified_days=3, facts=facts)
        self.assertTrue(eligible)

    def test_failed_inspection_blocks_but_old_one_ignored(self):
        base = {"daily": daily(3), "hazards": [], "assistances": [], "serious_events": []}
        facts = {**base, "inspections": [{"inspection_key": "i1", "inspection_date": "2026-08-20",
                                          "result": "fail", "finding_count": 1}]}
        eligible, _, _ = evaluate(
            self.criteria, settlement_date=self.day, consecutive_qualified_days=3, facts=facts)
        self.assertTrue(eligible)  # 超出 30 天回溯窗口
        facts = {**base, "inspections": [{"inspection_key": "i1", "inspection_date": "2026-09-20",
                                          "result": "fail", "finding_count": 1}]}
        eligible, _, _ = evaluate(
            self.criteria, settlement_date=self.day, consecutive_qualified_days=3, facts=facts)
        self.assertFalse(eligible)

    def test_assistance_never_blocks(self):
        facts = {"daily": daily(3), "hazards": [], "inspections": [],
                 "assistances": [{"assistance_key": "a1", "request_date": "2026-09-24",
                                  "topic": "咨询"}], "serious_events": []}
        eligible, reasons, metrics = evaluate(
            self.criteria, settlement_date=self.day, consecutive_qualified_days=3, facts=facts)
        self.assertTrue(eligible)
        self.assertEqual(1, metrics["assistances_in_lookback"])
        self.assertTrue(any("主动求助" in r for r in reasons))

    def test_serious_and_unresolved_emergency_veto(self):
        facts = {"daily": daily(3), "hazards": [], "inspections": [], "assistances": [],
                 "serious_events": [{"event_key": "e1", "event_date": "2026-09-24",
                                     "severity": "serious", "resolved": False}]}
        eligible, reasons, _ = evaluate(
            self.criteria, settlement_date=self.day, consecutive_qualified_days=3, facts=facts)
        self.assertFalse(eligible)
        self.assertIn("一票否决", "\n".join(reasons))


if __name__ == "__main__":
    unittest.main()
