import unittest

from relief_core.acceptance import run


class AcceptanceTest(unittest.TestCase):
    def test_offline_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        self.assertFalse(result["first_replayed"])
        self.assertTrue(result["second_replayed"])
        self.assertEqual(1, result["records"])
        self.assertTrue(result["eligible"])
        self.assertTrue(result["window_opened"])
        self.assertTrue(result["resettle_same_snapshot"])
        self.assertTrue(result["breached_without_erasing"])
        self.assertTrue(result["window_restored"])
        self.assertTrue(result["late_fact_corrected"])
        self.assertTrue(result["explains_review_date"])
        self.assertTrue(result["explains_exception_evidence"])


if __name__ == "__main__":
    unittest.main()
