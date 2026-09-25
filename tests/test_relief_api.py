import unittest
from datetime import datetime, timezone

from relief_core.api import relief_route
from relief_core.clock import FixedClock
from relief_core.qualification import DEFAULT_PARAMETERS
from relief_core.relief_service import ReliefService
from relief_core.service import DomainService
from relief_core.storage import Database


class ReliefApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.clock = FixedClock(datetime(2026, 9, 25, 1, 0, tzinfo=timezone.utc))
        self.domain = DomainService(self.database, self.clock)
        self.service = ReliefService(self.database, self.clock)
        self.domain.register_organization(request_id="org01", actor_id="bootstrap",
                                          organization_id="o1", name="企业一")
        self.domain.register_actor(request_id="adm01", actor_id="bootstrap", new_actor_id="a1",
                                   display_name="管理员", role="admin", organization_id="o1")
        self.domain.register_actor(request_id="rev01", actor_id="a1", new_actor_id="rv1",
                                   display_name="复核员", role="reviewer", organization_id="o1")
        self.domain.register_actor(request_id="op001", actor_id="a1", new_actor_id="op1",
                                   display_name="操作员", role="operator", organization_id="o1")
        self.domain.register_site(request_id="site01", actor_id="op1", site_id="s1",
                                  organization_id="o1", name="车间", timezone_name="Asia/Shanghai")

    def tearDown(self):
        self.database.close()

    def call(self, method, path, body=None, actor="op1"):
        return relief_route(self.service, method, path, body or {}, {"X-Actor-Id": actor})

    def publish_rule(self, effective="2026-09-01", overrides=None):
        parameters = dict(DEFAULT_PARAMETERS)
        parameters.update(overrides or {})
        status, payload = self.call("POST", "/rule-sets/propose",
                                    {"request_id": f"p-{effective}", "parameters": parameters,
                                     "effective_date": effective}, actor="a1")
        self.assertEqual(201, status, payload)
        rule_id = payload["response"]["rule_set_id"]
        self.assertEqual(200, self.call("POST", "/rule-sets/approve",
                                        {"request_id": f"a-{effective}", "rule_set_id": rule_id},
                                        actor="rv1")[0])
        self.assertEqual(200, self.call("POST", "/rule-sets/publish",
                                        {"request_id": f"u-{effective}", "rule_set_id": rule_id},
                                        actor="a1")[0])
        return rule_id

    def good_day(self, day, request_id):
        status, payload = self.call("POST", "/facts", {
            "request_id": request_id, "site_id": "s1", "channel": "daily_completion",
            "event_key": f"chk-{day}",
            "payload": {"on_time": True, "score": 95, "completed_count": 5, "required_count": 5},
            "business_date": day})
        self.assertEqual(201, status, payload)

    def settle(self, day, request_id):
        status, payload = self.call("POST", "/snapshots/settle",
                                    {"request_id": request_id, "site_id": "s1",
                                     "business_date": day})
        self.assertEqual(201, status, payload)
        return payload["response"]

    def test_unknown_relief_route_is_404(self):
        status, payload = self.call("GET", "/nope", None)
        self.assertEqual(404, status)
        self.assertEqual("route_not_found", payload["error"])

    def test_full_relief_flow_over_http(self):
        self.publish_rule(overrides={"min_streak_days": 3, "window_duration_days": 10,
                                     "review_interval_days": 5})
        for day in ["2026-09-23", "2026-09-24", "2026-09-25"]:
            self.good_day(day, f"fact-{day}")
            self.settle(day, f"settle-{day}")

        status, payload = self.call("GET", "/windows?site_id=s1")
        self.assertEqual(200, status)
        window_id = payload["items"][0]["window_id"]
        self.assertEqual("active", payload["items"][0]["status"])

        status, payload = self.call("GET", "/qualification/explain?site_id=s1", actor="a1")
        self.assertEqual(200, status)
        self.assertEqual("active", payload["window"]["effective_state"])
        self.assertEqual("2026-10-01", payload["next_review_date"])

        status, payload = self.call("POST", "/windows/suspend", {
            "request_id": "sus-1", "window_id": window_id, "reason_code": "targeted_tip",
            "reason_text": "实名举报", "evidence": {"references": ["doc://tip/1"]}}, actor="a1")
        self.assertEqual(200, status, payload)

        status, payload = self.call("POST", "/reviews/request",
                                    {"request_id": "rev-1", "window_id": window_id,
                                     "request_text": "举报不实"})
        self.assertEqual(201, status, payload)
        review_id = payload["response"]["review_id"]

        status, payload = self.call("POST", "/reviews/decide", {
            "request_id": "dec-1", "review_id": review_id, "decision": "reinstate",
            "decision_text": "举报不成立"}, actor="rv1")
        self.assertEqual(200, status, payload)

        status, payload = self.call("GET", f"/exceptions?window_id={window_id}", actor="a1")
        self.assertEqual(200, status)
        kinds = {item["kind"] for item in payload["items"]}
        self.assertIn("regulator_suspend", kinds)
        self.assertIn("review_reinstate", kinds)

    def test_settle_without_effective_rule_is_rejected(self):
        # 没有发布任何规则
        self.good_day("2026-09-23", "fact-only")
        status, payload = self.call("POST", "/snapshots/settle",
                                    {"request_id": "settle-norule", "site_id": "s1",
                                     "business_date": "2026-09-23"})
        self.assertEqual(400, status)
        self.assertEqual("validation_error", payload["error"])

    def test_suspend_without_evidence_is_rejected(self):
        self.publish_rule(overrides={"min_streak_days": 1})
        self.good_day("2026-09-25", "fact-25")
        self.settle("2026-09-25", "settle-25")
        window_id = self.call("GET", "/windows?site_id=s1")[1]["items"][0]["window_id"]
        status, payload = self.call("POST", "/windows/suspend", {
            "request_id": "sus-bad", "window_id": window_id,
            "reason_code": "targeted_tip", "reason_text": "举报", "evidence": {}}, actor="a1")
        self.assertEqual(400, status)
        self.assertEqual("validation_error", payload["error"])

    def test_replay_returns_200(self):
        self.publish_rule(overrides={"min_streak_days": 1})
        self.good_day("2026-09-25", "fact-r")
        first = self.settle("2026-09-25", "settle-r")
        status, payload = self.call("POST", "/snapshots/settle",
                                    {"request_id": "settle-r", "site_id": "s1",
                                     "business_date": "2026-09-25"})
        self.assertEqual(200, status)
        self.assertTrue(payload["replayed"])
        self.assertEqual(first["snapshot_id"], payload["response"]["snapshot_id"])


if __name__ == "__main__":
    unittest.main()
