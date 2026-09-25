import unittest
from datetime import datetime, timezone

from relief_core.api import route
from relief_core.clock import FixedClock
from relief_core.qualification_service import QualificationService
from relief_core.storage import Database

RULE = {"min_consecutive_qualified_days": 2, "qualified_ratio_days": 2,
        "required_qualified_ratio": 1.0, "window_duration_days": 5,
        "review_interval_days": 5, "hazard_lookback_days": 30,
        "inspection_lookback_days": 30, "serious_lookback_days": 30}


class QualificationApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.service = QualificationService(
            self.database, FixedClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc)))
        route(self.service, "POST", "/organizations",
              {"request_id": "org", "organization_id": "o1", "name": "局"},
              {"X-Actor-Id": "bootstrap"})
        route(self.service, "POST", "/actors",
              {"request_id": "a1", "new_actor_id": "a1", "display_name": "管理员",
               "role": "admin", "organization_id": "o1"},
              {"X-Actor-Id": "bootstrap"})
        for payload in (
            {"request_id": "op1", "new_actor_id": "op1", "display_name": "监管员",
             "role": "operator", "organization_id": "o1"},
            {"request_id": "rv1", "new_actor_id": "rv1", "display_name": "复核员",
             "role": "reviewer", "organization_id": "o1"},
        ):
            route(self.service, "POST", "/actors", payload, {"X-Actor-Id": "a1"})
        route(self.service, "POST", "/sites",
              {"request_id": "site", "site_id": "s1", "organization_id": "o1",
               "name": "厂", "timezone_name": "Asia/Shanghai"},
              {"X-Actor-Id": "op1"})

    def tearDown(self):
        self.database.close()

    def call(self, method, path, body=None, actor="op1"):
        return route(self.service, method, path, body or {},
                     {"X-Actor-Id": actor} if actor else {})

    def test_full_qualification_flow_over_http(self):
        status, body = self.call("POST", "/qualification-rules",
                                 {"request_id": "r1", "criteria": RULE}, actor="a1")
        self.assertEqual(201, status)
        rule_id = body["resource_id"]
        self.assertEqual(409, self.call("POST", "/qualification-rules/publish",
                                        {"request_id": "pub-x", "rule_id": rule_id,
                                         "effective_date": "2026-09-25"}, actor="a1")[0])
        self.assertEqual(201, self.call("POST", "/qualification-rules/approve",
                                        {"request_id": "ap", "rule_id": rule_id}, actor="rv1")[0])
        self.assertEqual(201, self.call("POST", "/qualification-rules/publish",
                                        {"request_id": "pub", "rule_id": rule_id,
                                         "effective_date": "2026-09-25"}, actor="a1")[0])
        for index, day in enumerate(("2026-09-24", "2026-09-25")):
            status, _ = self.call("POST", "/daily-reports",
                                  {"request_id": f"daily-{index}", "site_id": "s1",
                                   "report_date": day, "quality": "qualified",
                                   "submitted_at": f"{day}T09:00:00Z",
                                   "due_at": f"{day}T18:00:00Z"})
            self.assertEqual(201, status)
        status, body = self.call("POST", "/qualification-settlements",
                                 {"request_id": "set", "site_id": "s1",
                                  "settlement_date": "2026-09-25"})
        self.assertEqual(201, status)
        self.assertEqual("qualification_snapshot", body["resource_type"])

        status, body = self.call("GET", "/qualification?site_id=s1")
        self.assertEqual(200, status)
        self.assertTrue(body["eligible"])
        self.assertEqual("active", body["window_status"])
        self.assertTrue(any("下一次复核日" in line for line in body["explanation"]))

        # 紧急事件即时突破
        status, _ = self.call("POST", "/serious-events",
                              {"request_id": "e1", "site_id": "s1", "event_key": "ev-1",
                               "event_date": "2026-09-25",
                               "occurred_at": "2026-09-25T10:00:00Z",
                               "title": "突发泄漏", "severity": "emergency"})
        self.assertEqual(201, status)
        status, body = self.call("GET", "/qualification?site_id=s1")
        self.assertEqual("breached", body["window_status"])
        self.assertTrue(any("ev-1" in line for line in body["explanation"]))

        # 监管人员只能用受控例外码暂停/终止（此时已 breached，可直接终止）
        self.assertEqual(400, self.call("POST", "/window-terminations",
                                        {"request_id": "bad", "site_id": "s1",
                                         "reason_code": "WHATEVER", "reason_text": "x",
                                         "evidence": {"d": 1}})[0])
        status, body = self.call("POST", "/window-terminations",
                                 {"request_id": "t1", "site_id": "s1",
                                  "reason_code": "MAJOR_HAZARD_VERIFIED",
                                  "reason_text": "核实重大隐患",
                                  "evidence": {"hazard_key": "h-1"}})
        self.assertEqual(201, status)

        # 复核
        status, body = self.call("POST", "/reviews",
                                 {"request_id": "rv1-req", "site_id": "s1", "reason": "申请复核"})
        self.assertEqual(201, status)
        review_id = body["resource_id"]
        status, body = self.call("GET", "/reviews?site_id=s1")
        self.assertEqual(200, status)
        self.assertEqual("pending", body["items"][0]["status"])
        self.assertEqual(201, self.call("POST", "/reviews/decide",
                                        {"request_id": "d1", "review_id": review_id,
                                         "decision": "upheld",
                                         "decision_note": "维持终止决定"}, actor="rv1")[0])

        # 快照查询与审计链
        status, body = self.call("GET",
                                 "/qualification-snapshots?site_id=s1&date=2026-09-25")
        self.assertEqual(200, status)
        self.assertTrue(body["eligible"])
        status, body = self.call("GET", "/health")
        self.assertEqual(200, status)
        self.assertTrue(body["audit_valid"])

    def test_missing_site_id_is_400(self):
        self.assertEqual(400, self.call("GET", "/qualification")[0])
        self.assertEqual(400, self.call("GET", "/corrections")[0])

    def test_base_service_still_returns_404_for_new_routes(self):
        from relief_core.service import DomainService
        status, payload = route(DomainService(self.database), "GET", "/qualification?site_id=s1",
                                None)
        self.assertEqual(404, status)
        self.assertEqual("route_not_found", payload["error"])


if __name__ == "__main__":
    unittest.main()
